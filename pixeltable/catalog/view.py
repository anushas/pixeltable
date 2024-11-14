from __future__ import annotations

import inspect
import logging
from typing import TYPE_CHECKING, Any, Iterable, Literal, Optional
from uuid import UUID

import sqlalchemy.orm as orm

import pixeltable.exceptions as excs
import pixeltable.metadata.schema as md_schema
import pixeltable.type_system as ts
from pixeltable import catalog, exprs, func
from pixeltable.env import Env
from pixeltable.iterators import ComponentIterator

from .catalog import Catalog
from .column import Column
from .globals import _POS_COLUMN_NAME, UpdateStatus, MediaValidation
from .table import Table
from .table_version import TableVersion
from .table_version_path import TableVersionPath

if TYPE_CHECKING:
    import pixeltable as pxt

_logger = logging.getLogger('pixeltable')


class View(Table):
    """A `Table` that presents a virtual view of another table (or view).

    A view is typically backed by a store table, which records the view's columns and is joined back to the bases
    at query execution time.
    The exception is a snapshot view without a predicate and without additional columns: in that case, the view
    is simply a reference to a specific set of base versions.
    """
    def __init__(
            self, id: UUID, dir_id: UUID, name: str, tbl_version_path: TableVersionPath, base_id: UUID,
            snapshot_only: bool):
        super().__init__(id, dir_id, name, tbl_version_path)
        assert base_id in catalog.Catalog.get().tbl_dependents
        self._base_id = base_id  # keep a reference to the base Table ID, so that we can keep track of its dependents
        self._snapshot_only = snapshot_only

    @classmethod
    def _display_name(cls) -> str:
        return 'view'

    @classmethod
    def _create(
            cls, dir_id: UUID, name: str, base: TableVersionPath, additional_columns: dict[str, Any],
            predicate: Optional['pxt.exprs.Expr'], is_snapshot: bool, num_retained_versions: int, comment: str,
            media_validation: MediaValidation,
            iterator_cls: Optional[type[ComponentIterator]], iterator_args: Optional[dict]
    ) -> View:
        columns = cls._create_columns(additional_columns)
        cls._verify_schema(columns)

        # verify that filter can be evaluated in the context of the base
        if predicate is not None:
            if not predicate.is_bound_by(base):
                raise excs.Error(f'Filter cannot be computed in the context of the base {base.tbl_name()}')
            # create a copy that we can modify and store
            predicate = predicate.copy()

        # same for value exprs
        for col in columns:
            if not col.is_computed:
                continue
            # make sure that the value can be computed in the context of the base
            if col.value_expr is not None and not col.value_expr.is_bound_by(base):
                raise excs.Error(
                    f'Column {col.name}: value expression cannot be computed in the context of the base {base.tbl_name()}')

        if iterator_cls is not None:
            assert iterator_args is not None

            # validate iterator_args
            py_signature = inspect.signature(iterator_cls.__init__)
            try:
                # make sure iterator_args can be used to instantiate iterator_cls
                bound_args = py_signature.bind(None, **iterator_args).arguments  # None: arg for self
                # we ignore 'self'
                first_param_name = next(iter(py_signature.parameters))  # can't guarantee it's actually 'self'
                del bound_args[first_param_name]

                # construct Signature and type-check bound_args
                params = [
                    func.Parameter(param_name, param_type, kind=inspect.Parameter.POSITIONAL_OR_KEYWORD)
                    for param_name, param_type in iterator_cls.input_schema().items()
                ]
                sig = func.Signature(ts.InvalidType(), params)
                from pixeltable.exprs import FunctionCall
                FunctionCall.normalize_args(iterator_cls.__name__, sig, bound_args)
            except TypeError as e:
                raise excs.Error(f'Cannot instantiate iterator with given arguments: {e}')

            # prepend pos and output_schema columns to cols:
            # a component view exposes the pos column of its rowid;
            # we create that column here, so it gets assigned a column id;
            # stored=False: it is not stored separately (it's already stored as part of the rowid)
            iterator_cols = [Column(_POS_COLUMN_NAME, ts.IntType(), stored=False)]
            output_dict, unstored_cols = iterator_cls.output_schema(**bound_args)
            iterator_cols.extend([
                Column(col_name, col_type, stored=col_name not in unstored_cols)
                for col_name, col_type in output_dict.items()
            ])

            iterator_col_names = {col.name for col in iterator_cols}
            for col in columns:
                if col.name in iterator_col_names:
                    raise excs.Error(f'Duplicate name: column {col.name} is already present in the iterator output schema')
            columns = iterator_cols + columns

        with orm.Session(Env.get().engine, future=True) as session:
            from pixeltable.exprs import InlineDict
            iterator_args_expr: exprs.Expr = InlineDict(iterator_args) if iterator_args is not None else None
            iterator_class_fqn = f'{iterator_cls.__module__}.{iterator_cls.__name__}' if iterator_cls is not None \
                else None
            base_version_path = cls._get_snapshot_path(base) if is_snapshot else base
            base_versions = [
                (tbl_version.id.hex, tbl_version.version if is_snapshot or tbl_version.is_snapshot else None)
                for tbl_version in base_version_path.get_tbl_versions()
            ]

            # if this is a snapshot, we need to retarget all exprs to the snapshot tbl versions
            if is_snapshot:
                predicate = predicate.retarget(base_version_path) if predicate is not None else None
                iterator_args_expr = iterator_args_expr.retarget(base_version_path) \
                    if iterator_args_expr is not None else None
                for col in columns:
                    if col.value_expr is not None:
                        col.set_value_expr(col.value_expr.retarget(base_version_path))

            view_md = md_schema.ViewMd(
                is_snapshot=is_snapshot, predicate=predicate.as_dict() if predicate is not None else None,
                base_versions=base_versions,
                iterator_class_fqn=iterator_class_fqn,
                iterator_args=iterator_args_expr.as_dict() if iterator_args_expr is not None else None)

            id, tbl_version = TableVersion.create(
                session, dir_id, name, columns, num_retained_versions, comment, media_validation=media_validation,
                base_path=base_version_path, view_md=view_md)
            if tbl_version is None:
                # this is purely a snapshot: we use the base's tbl version path
                view = cls(id, dir_id, name, base_version_path, base.tbl_id(), snapshot_only=True)
                _logger.info(f'created snapshot {name}')
            else:
                view = cls(
                    id, dir_id, name, TableVersionPath(tbl_version, base=base_version_path), base.tbl_id(),
                    snapshot_only=False)
                _logger.info(f'Created view `{name}`, id={tbl_version.id}')

                from pixeltable.plan import Planner
                plan, num_values_per_row = Planner.create_view_load_plan(view._tbl_version_path)
                num_rows, num_excs, cols_with_excs = tbl_version.store_tbl.insert_rows(
                    plan, session.connection(), v_min=tbl_version.version)
                print(f'Created view `{name}` with {num_rows} rows, {num_excs} exceptions.')

            session.commit()
            cat = Catalog.get()
            cat.tbl_dependents[view._id] = []
            cat.tbl_dependents[base.tbl_id()].append(view)
            cat.tbls[view._id] = view
            return view

    @classmethod
    def _verify_column(
            cls, col: Column, existing_column_names: set[str], existing_query_names: Optional[set[str]] = None
    ) -> None:
        # make sure that columns are nullable or have a default
        if not col.col_type.nullable and not col.is_computed:
            raise excs.Error(f'Column {col.name}: non-computed columns in views must be nullable')
        super()._verify_column(col, existing_column_names, existing_query_names)

    @classmethod
    def _get_snapshot_path(cls, tbl_version_path: TableVersionPath) -> TableVersionPath:
        """Returns snapshot of the given table version path.
        All TableVersions of that path will be snapshot versions. Creates new versions from mutable versions,
        if necessary.
        """
        if tbl_version_path.is_snapshot():
            return tbl_version_path
        tbl_version = tbl_version_path.tbl_version
        if not tbl_version.is_snapshot:
            # create and register snapshot version
            tbl_version = tbl_version.create_snapshot_copy()
            assert tbl_version.is_snapshot

        return TableVersionPath(
            tbl_version,
            base=cls._get_snapshot_path(tbl_version_path.base) if tbl_version_path.base is not None else None)

    def _drop(self) -> None:
        cat = catalog.Catalog.get()
        # verify all dependents are deleted by now
        for dep in cat.tbl_dependents[self._id]:
            assert dep._is_dropped == True
        if self._snapshot_only:
            # there is not TableVersion to drop
            self._check_is_dropped()
            self.is_dropped = True
            with Env.get().engine.begin() as conn:
                TableVersion.delete_md(self._id, conn)
            # update catalog
            cat = catalog.Catalog.get()
            del cat.tbls[self._id]
        else:
            super()._drop()
        cat.tbl_dependents[self._base_id].remove(self)
        del cat.tbl_dependents[self._id]

    def get_metadata(self) -> dict[str, Any]:
        md = super().get_metadata()
        md['is_view'] = True
        md['is_snapshot'] = self._tbl_version_path.is_snapshot()
        return md

    def insert(
            self, rows: Optional[Iterable[dict[str, Any]]] = None, /, *, print_stats: bool = False,
            on_error: Literal['abort', 'ignore'] = 'abort', **kwargs: Any
    ) -> UpdateStatus:
        raise excs.Error(f'{self._display_name()} {self._name!r}: cannot insert into view')

    def delete(self, where: Optional[exprs.Expr] = None) -> UpdateStatus:
        raise excs.Error(f'{self._display_name()} {self._name!r}: cannot delete from view')
