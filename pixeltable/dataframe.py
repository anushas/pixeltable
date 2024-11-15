from __future__ import annotations

import builtins
import copy
import hashlib
import json
import logging
import mimetypes
import traceback
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Hashable, Iterator, Optional, Sequence, Union

import pandas as pd
import pandas.io.formats.style
import sqlalchemy as sql

import pixeltable.catalog as catalog
import pixeltable.exceptions as excs
import pixeltable.exprs as exprs
from pixeltable import exec
from pixeltable.catalog import is_valid_identifier
from pixeltable.catalog.globals import UpdateStatus
from pixeltable.env import Env
from pixeltable.plan import Planner
from pixeltable.type_system import ColumnType
from pixeltable.utils.formatter import Formatter
from pixeltable.utils.http_server import get_file_uri

if TYPE_CHECKING:
    import torch

__all__ = ['DataFrame']

_logger = logging.getLogger('pixeltable')


class DataFrameResultSet:
    def __init__(self, rows: list[list[Any]], schema: dict[str, ColumnType]):
        self._rows = rows
        self._col_names = list(schema.keys())
        self.__schema = schema
        self.__formatter = Formatter(len(self._rows), len(self._col_names), Env.get().http_address)

    @property
    def schema(self) -> dict[str, ColumnType]:
        return self.__schema

    def __len__(self) -> int:
        return len(self._rows)

    def __repr__(self) -> str:
        return self.to_pandas().__repr__()

    def _repr_html_(self) -> str:
        formatters: dict[Hashable, Callable[[object], str]] = {}
        for col_name, col_type in self.schema.items():
            formatter = self.__formatter.get_pandas_formatter(col_type)
            if formatter is not None:
                formatters[col_name] = formatter
        return self.to_pandas().to_html(formatters=formatters, escape=False, index=False)

    def __str__(self) -> str:
        return self.to_pandas().to_string()

    def _reverse(self) -> None:
        """Reverse order of rows"""
        self._rows.reverse()

    def to_pandas(self) -> pd.DataFrame:
        return pd.DataFrame.from_records(self._rows, columns=self._col_names)

    def _row_to_dict(self, row_idx: int) -> dict[str, Any]:
        return {self._col_names[i]: self._rows[row_idx][i] for i in range(len(self._col_names))}

    def __getitem__(self, index: Any) -> Any:
        if isinstance(index, str):
            if index not in self._col_names:
                raise excs.Error(f'Invalid column name: {index}')
            col_idx = self._col_names.index(index)
            return [row[col_idx] for row in self._rows]
        if isinstance(index, int):
            return self._row_to_dict(index)
        if isinstance(index, tuple) and len(index) == 2:
            if not isinstance(index[0], int) or not (isinstance(index[1], str) or isinstance(index[1], int)):
                raise excs.Error(f'Bad index, expected [<row idx>, <column name | column index>]: {index}')
            if isinstance(index[1], str) and index[1] not in self._col_names:
                raise excs.Error(f'Invalid column name: {index[1]}')
            col_idx = self._col_names.index(index[1]) if isinstance(index[1], str) else index[1]
            return self._rows[index[0]][col_idx]
        raise excs.Error(f'Bad index: {index}')

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return (self._row_to_dict(i) for i in range(len(self)))

    def __eq__(self, other):
        if not isinstance(other, DataFrameResultSet):
            return False
        return self.to_pandas().equals(other.to_pandas())


# # TODO: remove this; it's only here as a reminder that we still need to call release() in the current implementation
# class AnalysisInfo:
#     def __init__(self, tbl: catalog.TableVersion):
#         self.tbl = tbl
#         # output of the SQL scan stage
#         self.sql_scan_output_exprs: list[exprs.Expr] = []
#         # output of the agg stage
#         self.agg_output_exprs: list[exprs.Expr] = []
#         # Where clause of the Select stmt of the SQL scan stage
#         self.sql_where_clause: Optional[sql.ClauseElement] = None
#         # filter predicate applied to input rows of the SQL scan stage
#         self.filter: Optional[exprs.Predicate] = None
#         self.similarity_clause: Optional[exprs.ImageSimilarityPredicate] = None
#         self.agg_fn_calls: list[exprs.FunctionCall] = []  # derived from unique_exprs
#         self.has_frame_col: bool = False  # True if we're referencing the frame col
#
#         self.evaluator: Optional[exprs.Evaluator] = None
#         self.sql_scan_eval_ctx: list[exprs.Expr] = []  # needed to materialize output of SQL scan stage
#         self.agg_eval_ctx: list[exprs.Expr] = []  # needed to materialize output of agg stage
#         self.filter_eval_ctx: list[exprs.Expr] = []
#         self.group_by_eval_ctx: list[exprs.Expr] = []
#
#     def finalize_exec(self) -> None:
#         """
#         Call release() on all collected Exprs.
#         """
#         exprs.Expr.release_list(self.sql_scan_output_exprs)
#         exprs.Expr.release_list(self.agg_output_exprs)
#         if self.filter is not None:
#             self.filter.release()


class DataFrame:
    def __init__(
        self,
        tbl: catalog.TableVersionPath,
        select_list: Optional[list[tuple[exprs.Expr, Optional[str]]]] = None,
        where_clause: Optional[exprs.Expr] = None,
        group_by_clause: Optional[list[exprs.Expr]] = None,
        grouping_tbl: Optional[catalog.TableVersion] = None,
        order_by_clause: Optional[list[tuple[exprs.Expr, bool]]] = None,  # list[(expr, asc)]
        limit: Optional[int] = None,
    ):
        self.tbl = tbl

        # select list logic
        DataFrame._select_list_check_rep(select_list)  # check select list without expansion
        # exprs contain execution state and therefore cannot be shared
        select_list = copy.deepcopy(select_list)
        select_list_exprs, column_names = DataFrame._normalize_select_list(tbl, select_list)
        DataFrame._select_list_check_rep(list(zip(select_list_exprs, column_names)))
        # check select list after expansion to catch early
        # the following two lists are always non empty, even if select list is None.
        assert len(column_names) == len(select_list_exprs)
        self._select_list_exprs = select_list_exprs
        self._schema = {column_names[i]: select_list_exprs[i].col_type for i in range(len(column_names))}
        self.select_list = select_list

        self.where_clause = copy.deepcopy(where_clause)
        assert group_by_clause is None or grouping_tbl is None
        self.group_by_clause = copy.deepcopy(group_by_clause)
        self.grouping_tbl = grouping_tbl
        self.order_by_clause = copy.deepcopy(order_by_clause)
        self.limit_val = limit

    @classmethod
    def _select_list_check_rep(
        cls,
        select_list: Optional[list[tuple[exprs.Expr, Optional[str]]]],
    ) -> None:
        """Validate basic select list types."""
        if select_list is None:  # basic check for valid select list
            return

        assert len(select_list) > 0
        for ent in select_list:
            assert isinstance(ent, tuple)
            assert len(ent) == 2
            assert isinstance(ent[0], exprs.Expr)
            assert ent[1] is None or isinstance(ent[1], str)
            if isinstance(ent[1], str):
                assert is_valid_identifier(ent[1])

    @classmethod
    def _normalize_select_list(
        cls,
        tbl: catalog.TableVersionPath,
        select_list: Optional[list[tuple[exprs.Expr, Optional[str]]]],
    ) -> tuple[list[exprs.Expr], list[str]]:
        """
        Expand select list information with all columns and their names
        Returns:
            a pair composed of the list of expressions and the list of corresponding names
        """
        if select_list is None:
            select_list = [(exprs.ColumnRef(col), None) for col in tbl.columns()]

        out_exprs: list[exprs.Expr] = []
        out_names: list[str] = []  # keep track of order
        seen_out_names: set[str] = set()  # use to check for duplicates in loop, avoid square complexity
        for i, (expr, name) in enumerate(select_list):
            if name is None:
                # use default, add suffix if needed so default adds no duplicates
                default_name = expr.default_column_name()
                if default_name is not None:
                    column_name = default_name
                    if default_name in seen_out_names:
                        # already used, then add suffix until unique name is found
                        for j in range(1, len(out_names) + 1):
                            column_name = f'{default_name}_{j}'
                            if column_name not in seen_out_names:
                                break
                else:  # no default name, eg some expressions
                    column_name = f'col_{i}'
            else:  # user provided name, no attempt to rename
                column_name = name

            out_exprs.append(expr)
            out_names.append(column_name)
            seen_out_names.add(column_name)
        assert len(out_exprs) == len(out_names)
        assert set(out_names) == seen_out_names
        return out_exprs, out_names

    def _vars(self) -> dict[str, exprs.Variable]:
        """
        Return a dict mapping variable name to Variable for all Variables contained in any component of the DataFrame
        """
        all_exprs: list[exprs.Expr] = []
        all_exprs.extend(self._select_list_exprs)
        if self.where_clause is not None:
            all_exprs.append(self.where_clause)
        if self.group_by_clause is not None:
            all_exprs.extend(self.group_by_clause)
        if self.order_by_clause is not None:
            all_exprs.extend([expr for expr, _ in self.order_by_clause])
        vars = exprs.Expr.list_subexprs(all_exprs, expr_class=exprs.Variable)
        unique_vars: dict[str, exprs.Variable] = {}
        for var in vars:
            if var.name not in unique_vars:
                unique_vars[var.name] = var
            else:
                if unique_vars[var.name].col_type != var.col_type:
                    raise excs.Error(f'Multiple definitions of parameter {var.name}')
        return unique_vars

    def parameters(self) -> dict[str, ColumnType]:
        """Return a dict mapping parameter name to parameter type.

        Parameters are Variables contained in any component of the DataFrame.
        """
        vars = self._vars()
        return {name: var.col_type for name, var in vars.items()}

    def _exec(self, conn: Optional[sql.engine.Connection] = None) -> Iterator[exprs.DataRow]:
        """Run the query and return rows as a generator.
        This function must not modify the state of the DataFrame, otherwise it breaks dataset caching.
        """
        plan = self._create_query_plan()

        def exec_plan(conn: sql.engine.Connection) -> Iterator[exprs.DataRow]:
            plan.ctx.set_conn(conn)
            plan.open()
            try:
                for row_batch in plan:
                    yield from row_batch
            finally:
                plan.close()

        if conn is None:
            with Env.get().engine.begin() as conn:
                yield from exec_plan(conn)
        else:
            yield from exec_plan(conn)

    def _create_query_plan(self) -> exec.ExecNode:
        # construct a group-by clause if we're grouping by a table
        group_by_clause: Optional[list[exprs.Expr]] = None
        if self.grouping_tbl is not None:
            assert self.group_by_clause is None
            num_rowid_cols = len(self.grouping_tbl.store_tbl.rowid_columns())
            # the grouping table must be a base of self.tbl
            assert num_rowid_cols <= len(self.tbl.tbl_version.store_tbl.rowid_columns())
            group_by_clause = [exprs.RowidRef(self.tbl.tbl_version, idx) for idx in range(num_rowid_cols)]
        elif self.group_by_clause is not None:
            group_by_clause = self.group_by_clause

        for item in self._select_list_exprs:
            item.bind_rel_paths(None)

        return Planner.create_query_plan(
            self.tbl,
            self._select_list_exprs,
            where_clause=self.where_clause,
            group_by_clause=group_by_clause,
            order_by_clause=self.order_by_clause if self.order_by_clause is not None else [],
            limit=self.limit_val
        )


    def show(self, n: int = 20) -> DataFrameResultSet:
        assert n is not None
        return self.limit(n).collect()

    def head(self, n: int = 10) -> DataFrameResultSet:
        if self.order_by_clause is not None:
            raise excs.Error(f'head() cannot be used with order_by()')
        num_rowid_cols = len(self.tbl.tbl_version.store_tbl.rowid_columns())
        order_by_clause = [exprs.RowidRef(self.tbl.tbl_version, idx) for idx in range(num_rowid_cols)]
        return self.order_by(*order_by_clause, asc=True).limit(n).collect()

    def tail(self, n: int = 10) -> DataFrameResultSet:
        if self.order_by_clause is not None:
            raise excs.Error(f'tail() cannot be used with order_by()')
        num_rowid_cols = len(self.tbl.tbl_version.store_tbl.rowid_columns())
        order_by_clause = [exprs.RowidRef(self.tbl.tbl_version, idx) for idx in range(num_rowid_cols)]
        result = self.order_by(*order_by_clause, asc=False).limit(n).collect()
        result._reverse()
        return result

    @property
    def schema(self) -> dict[str, ColumnType]:
        return self._schema

    def bind(self, args: dict[str, Any]) -> DataFrame:
        """Bind arguments to parameters and return a new DataFrame."""
        # substitute Variables with the corresponding values according to 'args', converted to Literals
        select_list_exprs = copy.deepcopy(self._select_list_exprs)
        where_clause = copy.deepcopy(self.where_clause)
        group_by_clause = copy.deepcopy(self.group_by_clause)
        order_by_exprs = [copy.deepcopy(order_by_expr) for order_by_expr, _ in self.order_by_clause] \
            if self.order_by_clause is not None else None

        var_exprs: dict[exprs.Expr, exprs.Expr] = {}
        vars = self._vars()
        for arg_name, arg_val in args.items():
            if arg_name not in vars:
                # ignore unused variables
                continue
            var_expr = vars[arg_name]
            arg_expr = exprs.Expr.from_object(arg_val)
            if arg_expr is None:
                raise excs.Error(f'Cannot convert argument {arg_val} to a Pixeltable expression')
            var_exprs[var_expr] = arg_expr

        exprs.Expr.list_substitute(select_list_exprs, var_exprs)
        if where_clause is not None:
            where_clause.substitute(var_exprs)
        if group_by_clause is not None:
            exprs.Expr.list_substitute(group_by_clause, var_exprs)
        if order_by_exprs is not None:
            exprs.Expr.list_substitute(order_by_exprs, var_exprs)

        select_list = list(zip(select_list_exprs, self.schema.keys()))
        order_by_clause: Optional[list[tuple[exprs.Expr, bool]]] = None
        if order_by_exprs is not None:
            order_by_clause = [
                (expr, asc) for expr, asc in zip(order_by_exprs, [asc for _, asc in self.order_by_clause])
            ]

        return DataFrame(
            self.tbl, select_list=select_list, where_clause=where_clause,
            group_by_clause=group_by_clause, grouping_tbl=self.grouping_tbl,
            order_by_clause=order_by_clause, limit=self.limit_val)

    def _output_row_iterator(self, conn: Optional[sql.engine.Connection] = None) -> Iterator[list]:
        try:
            for data_row in self._exec(conn):
                yield [data_row[e.slot_idx] for e in self._select_list_exprs]
        except excs.ExprEvalError as e:
            msg = f'In row {e.row_num} the {e.expr_msg} encountered exception ' f'{type(e.exc).__name__}:\n{str(e.exc)}'
            if len(e.input_vals) > 0:
                input_msgs = [
                    f"'{d}' = {d.col_type.print_value(e.input_vals[i])}" for i, d in enumerate(e.expr.dependencies())
                ]
                msg += f'\nwith {", ".join(input_msgs)}'
            assert e.exc_tb is not None
            stack_trace = traceback.format_tb(e.exc_tb)
            if len(stack_trace) > 2:
                # append a stack trace if the exception happened in user code
                # (frame 0 is ExprEvaluator and frame 1 is some expr's eval()
                nl = '\n'
                # [-1:0:-1]: leave out entry 0 and reverse order, so that the most recent frame is at the top
                msg += f'\nStack:\n{nl.join(stack_trace[-1:1:-1])}'
            raise excs.Error(msg)
        except sql.exc.DBAPIError as e:
            raise excs.Error(f'Error during SQL execution:\n{e}')

    def collect(self) -> DataFrameResultSet:
        return self._collect()

    def _collect(self, conn: Optional[sql.engine.Connection] = None) -> DataFrameResultSet:
        return DataFrameResultSet(list(self._output_row_iterator(conn)), self.schema)

    def count(self) -> int:
        from pixeltable.plan import Planner

        stmt = Planner.create_count_stmt(self.tbl, self.where_clause)
        with Env.get().engine.connect() as conn:
            result: int = conn.execute(stmt).scalar_one()
            assert isinstance(result, int)
            return result

    def _description(self) -> pd.DataFrame:
        """see DataFrame.describe()"""
        heading_vals: list[str] = []
        info_vals: list[str] = []
        if self.select_list is not None:
            assert len(self.select_list) > 0
            heading_vals.append('Select')
            heading_vals.extend([''] * (len(self.select_list) - 1))
            info_vals.extend(self.schema.keys())
        if self.where_clause is not None:
            heading_vals.append('Where')
            info_vals.append(self.where_clause.display_str(inline=False))
        if self.group_by_clause is not None:
            heading_vals.append('Group By')
            heading_vals.extend([''] * (len(self.group_by_clause) - 1))
            info_vals.extend([e.display_str(inline=False) for e in self.group_by_clause])
        if self.order_by_clause is not None:
            heading_vals.append('Order By')
            heading_vals.extend([''] * (len(self.order_by_clause) - 1))
            info_vals.extend(
                [f'{e[0].display_str(inline=False)} {"asc" if e[1] else "desc"}' for e in self.order_by_clause]
            )
        if self.limit_val is not None:
            heading_vals.append('Limit')
            info_vals.append(str(self.limit_val))
        assert len(heading_vals) > 0
        assert len(info_vals) > 0
        assert len(heading_vals) == len(info_vals)
        return pd.DataFrame({'Heading': heading_vals, 'Info': info_vals})

    def _description_html(self) -> pandas.io.formats.style.Styler:
        """Return the description in an ipython-friendly manner."""
        pd_df = self._description()
        # white-space: pre-wrap: print \n as newline
        # th: center-align headings
        return (
            pd_df.style.set_properties(None, **{'white-space': 'pre-wrap', 'text-align': 'left'})
            .set_table_styles([dict(selector='th', props=[('text-align', 'center')])])
            .hide(axis='index')
            .hide(axis='columns')
        )

    def describe(self) -> None:
        """
        Prints a tabular description of this DataFrame.
        The description has two columns, heading and info, which list the contents of each 'component'
                (select list, where clause, ...) vertically.
        """
        if getattr(builtins, '__IPYTHON__', False):
            from IPython.display import display
            display(self._description_html())
        else:
            print(self.__repr__())

    def __repr__(self) -> str:
        return self._description().to_string(header=False, index=False)

    def _repr_html_(self) -> str:
        return self._description_html()._repr_html_()  # type: ignore[attr-defined]

    def select(self, *items: Any, **named_items: Any) -> DataFrame:
        if self.select_list is not None:
            raise excs.Error(f'Select list already specified')
        for name, _ in named_items.items():
            if not isinstance(name, str) or not is_valid_identifier(name):
                raise excs.Error(f'Invalid name: {name}')
        base_list = [(expr, None) for expr in items] + [(expr, k) for (k, expr) in named_items.items()]
        if len(base_list) == 0:
            return self

        # analyze select list; wrap literals with the corresponding expressions
        select_list = []
        for raw_expr, name in base_list:
            if isinstance(raw_expr, exprs.Expr):
                select_list.append((raw_expr, name))
            elif isinstance(raw_expr, dict):
                select_list.append((exprs.InlineDict(raw_expr), name))
            elif isinstance(raw_expr, list):
                select_list.append((exprs.InlineList(raw_expr), name))
            else:
                select_list.append((exprs.Literal(raw_expr), name))
            expr = select_list[-1][0]
            if expr.col_type.is_invalid_type():
                raise excs.Error(f'Invalid type: {raw_expr}')
            # TODO: check that ColumnRefs in expr refer to self.tbl

        # check user provided names do not conflict among themselves
        # or with auto-generated ones
        seen: set[str] = set()
        _, names = DataFrame._normalize_select_list(self.tbl, select_list)
        for name in names:
            if name in seen:
                repeated_names = [j for j, x in enumerate(names) if x == name]
                pretty = ', '.join(map(str, repeated_names))
                raise excs.Error(f'Repeated column name "{name}" in select() at positions: {pretty}')
            seen.add(name)

        return DataFrame(
            self.tbl,
            select_list=select_list,
            where_clause=self.where_clause,
            group_by_clause=self.group_by_clause,
            grouping_tbl=self.grouping_tbl,
            order_by_clause=self.order_by_clause,
            limit=self.limit_val,
        )

    def where(self, pred: exprs.Expr) -> DataFrame:
        if not isinstance(pred, exprs.Expr):
            raise excs.Error(f'Where() requires a Pixeltable expression, but instead got {type(pred)}')
        if not pred.col_type.is_bool_type():
            raise excs.Error(f'Where(): expression needs to return bool, but instead returns {pred.col_type}')
        return DataFrame(
            self.tbl,
            select_list=self.select_list,
            where_clause=pred,
            group_by_clause=self.group_by_clause,
            grouping_tbl=self.grouping_tbl,
            order_by_clause=self.order_by_clause,
            limit=self.limit_val,
        )

    def group_by(self, *grouping_items: Any) -> DataFrame:
        """Add a group-by clause to this DataFrame.
        Variants:
        - group_by(<base table>): group a component view by their respective base table rows
        - group_by(<expr>, ...): group by the given expressions
        """
        if self.group_by_clause is not None:
            raise excs.Error(f'Group-by already specified')
        grouping_tbl: Optional[catalog.TableVersion] = None
        group_by_clause: Optional[list[exprs.Expr]] = None
        for item in grouping_items:
            if isinstance(item, catalog.Table):
                if len(grouping_items) > 1:
                    raise excs.Error(f'group_by(): only one table can be specified')
                # we need to make sure that the grouping table is a base of self.tbl
                base = self.tbl.find_tbl_version(item._tbl_version_path.tbl_id())
                if base is None or base.id == self.tbl.tbl_id():
                    raise excs.Error(f'group_by(): {item._name} is not a base table of {self.tbl.tbl_name()}')
                grouping_tbl = item._tbl_version_path.tbl_version
                break
            if not isinstance(item, exprs.Expr):
                raise excs.Error(f'Invalid expression in group_by(): {item}')
        if grouping_tbl is None:
            group_by_clause = list(grouping_items)
        return DataFrame(
            self.tbl,
            select_list=self.select_list,
            where_clause=self.where_clause,
            group_by_clause=group_by_clause,
            grouping_tbl=grouping_tbl,
            order_by_clause=self.order_by_clause,
            limit=self.limit_val,
        )

    def order_by(self, *expr_list: exprs.Expr, asc: bool = True) -> DataFrame:
        for e in expr_list:
            if not isinstance(e, exprs.Expr):
                raise excs.Error(f'Invalid expression in order_by(): {e}')
        order_by_clause = self.order_by_clause if self.order_by_clause is not None else []
        order_by_clause.extend([(e.copy(), asc) for e in expr_list])
        return DataFrame(
            self.tbl,
            select_list=self.select_list,
            where_clause=self.where_clause,
            group_by_clause=self.group_by_clause,
            grouping_tbl=self.grouping_tbl,
            order_by_clause=order_by_clause,
            limit=self.limit_val,
        )

    def limit(self, n: int) -> DataFrame:
        # TODO: allow n to be a Variable that can be substituted in bind()
        assert n is not None and isinstance(n, int)
        return DataFrame(
            self.tbl,
            select_list=self.select_list,
            where_clause=self.where_clause,
            group_by_clause=self.group_by_clause,
            grouping_tbl=self.grouping_tbl,
            order_by_clause=self.order_by_clause,
            limit=n,
        )

    def update(self, value_spec: dict[str, Any], cascade: bool = True) -> UpdateStatus:
        self._validate_mutable('update')
        return self.tbl.tbl_version.update(value_spec, where=self.where_clause, cascade=cascade)

    def delete(self) -> UpdateStatus:
        self._validate_mutable('delete')
        if not self.tbl.is_insertable():
            raise excs.Error(f'Cannot delete from view')
        return self.tbl.tbl_version.delete(where=self.where_clause)

    def _validate_mutable(self, op_name: str) -> None:
        """Tests whether this `DataFrame` can be mutated (such as by an update operation)."""
        if self.group_by_clause is not None or self.grouping_tbl is not None:
            raise excs.Error(f'Cannot use `{op_name}` after `group_by`')
        if self.order_by_clause is not None:
            raise excs.Error(f'Cannot use `{op_name}` after `order_by`')
        if self.select_list is not None:
            raise excs.Error(f'Cannot use `{op_name}` after `select`')
        if self.limit_val is not None:
            raise excs.Error(f'Cannot use `{op_name}` after `limit`')

    def __getitem__(self, index: Union[exprs.Expr, Sequence[exprs.Expr]]) -> DataFrame:
        """
        Allowed:
        - [list[Expr]]/[tuple[Expr]]: setting the select list
        - [Expr]: setting a single-col select list
        """
        if isinstance(index, exprs.Expr):
            return self.select(index)
        if isinstance(index, Sequence):
            return self.select(*index)
        raise TypeError(f'Invalid index type: {type(index)}')

    def as_dict(self) -> dict[str, Any]:
        """
        Returns:
            Dictionary representing this dataframe.
        """
        tbl_versions = self.tbl.get_tbl_versions()
        d = {
            '_classname': 'DataFrame',
            'tbl': self.tbl.as_dict(),
            'select_list':
                [(e.as_dict(), name) for (e, name) in self.select_list] if self.select_list is not None else None,
            'where_clause': self.where_clause.as_dict() if self.where_clause is not None else None,
            'group_by_clause':
                [e.as_dict() for e in self.group_by_clause] if self.group_by_clause is not None else None,
            'grouping_tbl': self.grouping_tbl.as_dict() if self.grouping_tbl is not None else None,
            'order_by_clause':
                [(e.as_dict(), asc) for (e,asc) in self.order_by_clause] if self.order_by_clause is not None else None,
            'limit_val': self.limit_val,
        }
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> 'DataFrame':
        tbl = catalog.TableVersionPath.from_dict(d['tbl'])
        select_list = [(exprs.Expr.from_dict(e), name) for e, name in d['select_list']] \
            if d['select_list'] is not None else None
        where_clause = exprs.Expr.from_dict(d['where_clause']) \
            if d['where_clause'] is not None else None
        group_by_clause = [exprs.Expr.from_dict(e) for e in d['group_by_clause']] \
            if d['group_by_clause'] is not None else None
        grouping_tbl = catalog.TableVersion.from_dict(d['grouping_tbl']) \
            if d['grouping_tbl'] is not None else None
        order_by_clause = [(exprs.Expr.from_dict(e), asc) for e, asc in d['order_by_clause']] \
            if d['order_by_clause'] is not None else None
        limit_val = d['limit_val']
        return DataFrame(
            tbl, select_list=select_list, where_clause=where_clause, group_by_clause=group_by_clause,
            grouping_tbl=grouping_tbl, order_by_clause=order_by_clause, limit=limit_val)

    def _hash_result_set(self) -> str:
        """Return a hash that changes when the result set changes."""
        d = self.as_dict()
        # add list of referenced table versions (the actual versions, not the effective ones) in order to force cache
        # invalidation when any of the referenced tables changes
        d['tbl_versions'] = [tbl_version.version for tbl_version in self.tbl.get_tbl_versions()]
        summary_string = json.dumps(d)
        return hashlib.sha256(summary_string.encode()).hexdigest()

    def to_coco_dataset(self) -> Path:
        """Convert the dataframe to a COCO dataset.
        This dataframe must return a single json-typed output column in the following format:
        {
            'image': PIL.Image.Image,
            'annotations': [
                {
                    'bbox': [x: int, y: int, w: int, h: int],
                    'category': str | int,
                },
                ...
            ],
        }

        Returns:
            Path to the COCO dataset file.
        """
        from pixeltable.utils.coco import write_coco_dataset

        cache_key = self._hash_result_set()
        dest_path = Env.get().dataset_cache_dir / f'coco_{cache_key}'
        if dest_path.exists():
            assert dest_path.is_dir()
            data_file_path = dest_path / 'data.json'
            assert data_file_path.exists()
            assert data_file_path.is_file()
            return data_file_path
        else:
            return write_coco_dataset(self, dest_path)

    # TODO Factor this out into a separate module.
    # The return type is unresolvable, but torch can't be imported since it's an optional dependency.
    def to_pytorch_dataset(self, image_format: str = 'pt') -> 'torch.utils.data.IterableDataset':
        """
        Convert the dataframe to a pytorch IterableDataset suitable for parallel loading
        with torch.utils.data.DataLoader.

        This method requires pyarrow >= 13, torch and torchvision to work.

        This method serializes data so it can be read from disk efficiently and repeatedly without
        re-executing the query. This data is cached to disk for future re-use.

        Args:
            image_format: format of the images. Can be 'pt' (pytorch tensor) or 'np' (numpy array).
                    'np' means image columns return as an RGB uint8 array of shape HxWxC.
                    'pt' means image columns return as a CxHxW tensor with values in [0,1] and type torch.float32.
                        (the format output by torchvision.transforms.ToTensor())

        Returns:
            A pytorch IterableDataset: Columns become fields of the dataset, where rows are returned as a dictionary
                compatible with torch.utils.data.DataLoader default collation.

        Constraints:
            The default collate_fn for torch.data.util.DataLoader cannot represent null values as part of a
            pytorch tensor when forming batches. These values will raise an exception while running the dataloader.

            If you have them, you can work around None values by providing your custom collate_fn to the DataLoader
            (and have your model handle it). Or, if these are not meaningful values within a minibtach, you can
            modify or remove any such values through selections and filters prior to calling to_pytorch_dataset().
        """
        # check dependencies
        Env.get().require_package('pyarrow', [13])
        Env.get().require_package('torch')
        Env.get().require_package('torchvision')

        from pixeltable.io.parquet import save_parquet
        from pixeltable.utils.pytorch import PixeltablePytorchDataset

        cache_key = self._hash_result_set()

        dest_path = (Env.get().dataset_cache_dir / f'df_{cache_key}').with_suffix('.parquet')
        if dest_path.exists():  # fast path: use cache
            assert dest_path.is_dir()
        else:
            save_parquet(self, dest_path)

        return PixeltablePytorchDataset(path=dest_path, image_format=image_format)
