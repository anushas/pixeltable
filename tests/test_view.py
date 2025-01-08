import datetime
import logging

import PIL
import pytest
import re

import pixeltable as pxt
from pixeltable import catalog
from pixeltable import exceptions as excs

from .utils import (assert_resultset_eq, create_test_tbl, reload_catalog, validate_update_status, ReloadTester)

logger = logging.getLogger('pixeltable')

class TestView:
    """
    TODO:
    - test tree of views
    - test consecutive component views

    """
    def create_tbl(self) -> catalog.InsertableTable:
        """Create table with computed columns"""
        t = create_test_tbl()
        t.add_column(d1=t.c3 - 1)
        # add column that can be updated
        t.add_column(c10=pxt.Float)
        t.update({'c10': t.c3})
        # computed column that depends on two columns: exercise duplicate elimination during query construction
        t.add_column(d2=t.c3 - t.c10)
        return t

    def test_errors(self, reset_db) -> None:
        t = self.create_tbl()
        assert t._base is None

        v = pxt.create_view('test_view', t)
        with pytest.raises(excs.Error) as exc_info:
            _ = v.insert([{'bad_col': 1}])
        assert 'cannot insert into view' in str(exc_info.value)
        with pytest.raises(excs.Error) as exc_info:
            _ = v.insert(bad_col=1)
        assert 'cannot insert into view' in str(exc_info.value)
        with pytest.raises(excs.Error) as exc_info:
            _ = v.delete()
        assert 'cannot delete from view' in str(exc_info.value)

        with pytest.raises(excs.Error) as exc_info:
            u = pxt.create_table('joined_tbl', {'c1': pxt.String})
            join_df = t.join(u, on=t.c1 == u.c1)
            _ = pxt.create_view('join_view', join_df)
        assert 'cannot create a view of a join' in str(exc_info.value).lower()

    def test_basic(self, reset_db) -> None:
        t = self.create_tbl()
        assert t._base is None

        # create view with filter and computed columns
        schema = {
            'v1': t.c3 * 2.0,
            'v2': t.c6.f5,
        }
        v = pxt.create_view('test_view', t.where(t.c2 < 10), additional_columns=schema)
        # TODO: test repr more thoroughly
        _ = v.__repr__()
        assert_resultset_eq(
            v.select(v.v1).order_by(v.c2).collect(),
            t.select(t.c3 * 2.0).where(t.c2 < 10).order_by(t.c2).collect())
        # view-only query; returns the same result
        assert_resultset_eq(
            v.select(v.v1).order_by(v.v1).collect(),
            t.select(t.c3 * 2.0).where(t.c2 < 10).order_by(t.c2).collect())
        # computed columns that don't reference the base table
        v.add_column(v3=v.v1 * 2.0)
        v.add_column(v4=v.v2[0])

        def check_view(t: pxt.Table, v: pxt.Table) -> None:
            assert v._base == t
            assert v.count() == t.where(t.c2 < 10).count()
            assert_resultset_eq(
                v.select(v.v1).order_by(v.c2).collect(),
                t.select(t.c3 * 2.0).where(t.c2 < 10).order_by(t.c2).collect())
            assert_resultset_eq(
                v.select(v.v3).order_by(v.c2).collect(),
                t.select(t.c3 * 4.0).where(t.c2 < 10).order_by(t.c2).collect())
            assert_resultset_eq(
                v.select(v.v4).order_by(v.c2).collect(),
                t.select(t.c6.f5[0]).where(t.c2 < 10).order_by(t.c2).collect())
        check_view(t, v)

        # check view md after reload
        reload_catalog()
        t = pxt.get_table('test_tbl')
        v = pxt.get_table('test_view')
        check_view(t, v)

        view_query = v.select(v.v1).order_by(v.c2)
        base_query = t.select(t.c3 * 2.0).where(t.c2 < 10).order_by(t.c2)

        # insert data: of 20 new rows, only 10 are reflected in the view
        rows = list(t.select(t.c1, t.c1n, t.c2, t.c3, t.c4, t.c5, t.c6, t.c7, t.c10).where(t.c2 < 20).collect())
        status = t.insert(rows)
        assert status.num_rows == 30
        assert t.count() == 120
        check_view(t, v)

        # update data: cascade to view
        status = t.update({'c4': True, 'c3': t.c3 + 1.0, 'c10': t.c10 - 1.0}, where=t.c2 < 5, cascade=True)
        assert status.num_rows == 10 * 2  # *2: rows affected in both base table and view
        assert t.count() == 120
        check_view(t, v)

        # base table delete is reflected in view
        status = t.delete(where=t.c2 < 5)
        status.num_rows == 10 * 2  # *2: rows affected in both base table and view
        assert t.count() == 110
        check_view(t, v)

        # check alternate view creation syntax (via a DataFrame)
        v2 = pxt.create_view('test_view_alt', t.where(t.c2 < 10), additional_columns=schema)
        validate_update_status(v2.add_column(v3=v2.v1 * 2.0), expected_rows=10)
        validate_update_status(v2.add_column(v4=v2.v2[0]), expected_rows=10)
        check_view(t, v2)

        # test delete view
        pxt.drop_table('test_view')
        with pytest.raises(excs.Error) as exc_info:
            _ = pxt.get_table('test_view')
        assert 'No such path:' in str(exc_info.value)
        reload_catalog()
        # still true after reload
        with pytest.raises(excs.Error) as exc_info:
            _ = pxt.get_table('test_view')
        assert 'No such path:' in str(exc_info.value)

        t = pxt.get_table('test_tbl')
        with pytest.raises(excs.Error) as exc_info:
            _ = pxt.create_view('lambda_view', t, additional_columns={'v1': lambda c3: c3 * 2.0})
        assert "invalid value for column 'v1'" in str(exc_info.value).lower()

    def test_create_if_exists(self, reset_db, reload_tester: ReloadTester) -> None:
        """ Test if_exists parameter of create_view API"""
        t = self.create_tbl()
        v = pxt.create_view('test_view', t)
        id_before = v._id

        # invalid if_exists value is rejected
        with pytest.raises(excs.Error) as exc_info:
            pxt.create_view('test_view', t, if_exists='invalid')
        assert "if_exists must be one of: ['error', 'ignore', 'replace', 'replace_force']" in str(exc_info.value).lower()

        # scenario 1: a view exists at the path already
        expected_err = "already exists"
        with pytest.raises(pxt.Error, match=re.compile(expected_err, re.IGNORECASE)):
            pxt.create_view('test_view', t)
        # if_exists='ignore' should return the existing view
        v2 = pxt.create_view('test_view', t, if_exists='ignore')
        assert v2 == v
        assert v2._id == id_before
        # if_exists='replace' should drop the existing view and create a new one
        v2 = pxt.create_view('test_view', t, if_exists='replace')
        assert v2 != v
        assert v2._id != id_before
        id_before = v2._id

        # scenario 2: a view exists at the path, but has dependency
        v_on_v = pxt.create_view('test_view_on_view', v2)
        with pytest.raises(pxt.Error, match=re.compile(expected_err, re.IGNORECASE)):
            pxt.create_view('test_view', t)
        # if_exists='ignore' should return the existing view
        v3 = pxt.create_view('test_view', t, if_exists='ignore')
        assert v3 == v2
        assert v3._id == id_before
        assert 'test_view_on_view' in pxt.list_tables()
        # if_exists='replace' cannot drop a view with a dependent view.
        # it should raise an error and recommend using 'replace_force'
        with pytest.raises(excs.Error) as exc_info:
            pxt.create_view('test_view', t, if_exists='replace')
        err_msg = str(exc_info.value).lower()
        assert 'already exists' in err_msg and 'has dependents' in err_msg and 'replace_force' in err_msg
        assert 'test_view_on_view' in pxt.list_tables()
        # if_exists='replace_force' should drop the existing view and
        # its dependent views and create a new one
        v3 = pxt.create_view('test_view', t, if_exists='replace_force')
        assert v3 != v2
        assert v3._id != id_before
        assert v_on_v._is_dropped
        assert 'test_view_on_view' not in pxt.list_tables()

        # scenario 3: path exists but is not a view
        _ = pxt.create_table('not_view', {'c1': pxt.String})
        with pytest.raises(pxt.Error, match=re.compile(expected_err, re.IGNORECASE)):
            pxt.create_view('not_view', t)
        for _ie in ['ignore', 'replace', 'replace_force']:
            with pytest.raises(excs.Error) as exc_info:
                pxt.create_view('not_view', t, if_exists=_ie)
            err_msg = str(exc_info.value).lower()
            assert 'already exists' in err_msg and 'is not a view' in err_msg
            assert 'not_view' in pxt.list_tables(), f"with if_exists={_ie}"

        # sanity check persistence
        _ = reload_tester.run_query(t.select())
        _ = reload_tester.run_query(v3.select())
        reload_tester.run_reload_test()

    def test_add_column_to_view(self, reset_db, test_tbl: catalog.Table, reload_tester: ReloadTester) -> None:
        """ Test add_column* methods for views """
        t = test_tbl
        t_c1_val0 = t.select(t.c1).order_by(t.c1).collect()[0]['c1']

        # adding column with same name as a base table column at
        # the time of creating a view will raise an error now.
        with pytest.raises(excs.Error, match=re.compile("column 'c1' already exists in the base table", re.IGNORECASE)):
            pxt.create_view('test_view', t, additional_columns={'c1': pxt.Int})

        # create a view and add a column with default value
        v = pxt.create_view('test_view', t, additional_columns={'v1': pxt.Int})
        v.add_column(vcol='xxx')
        assert 'vcol' in v.columns
        assert v.select(v.vcol).collect()[0]['vcol'] == 'xxx'

        # add column with same name as an existing column.
        # the result will depend on the if_exists parameter.
        # test with the existing column specific to the view, or a base table column.
        self._test_add_column_if_exists(v, t, 'vcol', 'xxx', is_base_column=False)
        _ = reload_tester.run_query(v.select())
        reload_tester.run_reload_test()

        self._test_add_column_if_exists(v, t, 'c1', t_c1_val0, is_base_column=True)
        _ = reload_tester.run_query(v.select())
        reload_tester.run_reload_test()

    def _test_add_column_if_exists(
        self, v: catalog.View, t: catalog.Table, col_name: str,
        orig_val: str, is_base_column: bool
    ) -> None:
        """ Test if_exists parameter of the add column methods for views """
        non_existing_col1 = 'non_existing1_' + col_name
        non_existing_col2 = 'non_existing2_' + col_name
        non_existing_col3 = 'non_existing3_' + col_name
        non_existing_col4 = 'non_existing4_' + col_name
        non_existing_col5 = 'non_existing5_' + col_name

        # invalid if_exists value is rejected
        expected_error = "if_exists must be one of: ['error', 'ignore', 'replace', 'replace_force']"
        with pytest.raises(excs.Error, match=re.compile(re.escape(expected_error), re.IGNORECASE)):
            v.add_column(**{col_name: pxt.Int}, if_exists='invalid')
        with pytest.raises(excs.Error, match=re.compile(re.escape(expected_error), re.IGNORECASE)):
            v.add_computed_column(**{col_name: t.c2 + t.c3}, if_exists='invalid')
        with pytest.raises(excs.Error, match=re.compile(re.escape(expected_error), re.IGNORECASE)):
            v.add_columns({col_name: pxt.Int, non_existing_col1: pxt.String}, if_exists='invalid')
        assert col_name in v.columns
        assert v.select().collect()[0][col_name] == orig_val

        # by default, raises an error if the column already exists
        expected_error = f"duplicate column name: '{col_name}'"
        with pytest.raises(excs.Error, match=re.compile(expected_error, re.IGNORECASE)):
            v.add_column(**{col_name: pxt.Int})
        with pytest.raises(excs.Error, match=re.compile(expected_error, re.IGNORECASE)):
            v.add_computed_column(**{col_name: t.c2 + t.c3})
        with pytest.raises(excs.Error, match=re.compile(expected_error, re.IGNORECASE)):
            v.add_columns({col_name: pxt.Int, non_existing_col2: pxt.String})
        assert col_name in v.columns
        assert v.select(getattr(v, col_name)).collect()[0][col_name] == orig_val
        assert non_existing_col2 not in v.columns

        # if_exists='ignore' will not add the column if it already exists
        v.add_column(**{col_name: pxt.Int}, if_exists='ignore')
        assert col_name in v.columns
        assert v.select().collect()[0][col_name] == orig_val
        v.add_computed_column(**{col_name: t.c2 + t.c3}, if_exists='ignore')
        assert col_name in v.columns
        assert v.select().collect()[0][col_name] == orig_val
        v.add_columns({col_name: pxt.Int, non_existing_col2: pxt.String}, if_exists='ignore')
        assert col_name in v.columns
        assert v.select().collect()[0][col_name] == orig_val
        assert non_existing_col2 in v.columns

        # if_exists='replace' will replace the column if it already exists.
        # for a column specific to view. For a base table column, it will raise an error.
        if is_base_column:
            with pytest.raises(excs.Error) as exc_info:
                v.add_column(**{col_name: pxt.String}, if_exists='replace')
            error_msg = str(exc_info.value).lower()
            assert "is a base table column" in error_msg and "cannot replace" in error_msg
            assert col_name in v.columns
            assert v.select().collect()[0][col_name] == orig_val
            with pytest.raises(excs.Error) as exc_info:
                v.add_computed_column(**{col_name: t.c2 + t.c3}, if_exists='replace')
            error_msg = str(exc_info.value).lower()
            assert "is a base table column" in error_msg and "cannot replace" in error_msg
            assert col_name in v.columns
            assert v.select(getattr(v, col_name)).collect()[0][col_name] == orig_val
            with pytest.raises(excs.Error) as exc_info:
                v.add_columns({col_name: pxt.String, non_existing_col3: pxt.String}, if_exists='replace')
            error_msg = str(exc_info.value).lower()
            assert "is a base table column" in error_msg and "cannot replace" in error_msg
            assert col_name in v.columns
            assert v.select(getattr(v, col_name)).collect()[0][col_name] == orig_val
            assert non_existing_col3 not in v.columns
        else:
            v.add_columns({col_name: pxt.Int, non_existing_col4: pxt.String}, if_exists='replace')
            assert col_name in v.columns
            assert v.select(getattr(v, col_name)).collect()[0][col_name] is None
            assert non_existing_col4 in v.columns
            v.add_column(**{col_name: 'aaa'}, if_exists='replace')
            assert col_name in v.columns
            assert v.select(getattr(v, col_name)).collect()[0][col_name] == 'aaa'
            v.add_computed_column(**{col_name: t.c2 + t.c3}, if_exists='replace')
            assert col_name in v.columns
            row0 = v.select().collect()[0]
            assert row0[col_name] == row0['c2'] + row0['c3']

            # if_exists='replace' will raise an error and not replace if the column has a dependency.
            col_ref = getattr(v, col_name)
            v.add_column(**{non_existing_col5: col_ref + 12.3})
            assert v.select(getattr(v, non_existing_col5)).collect()[0][non_existing_col5] == row0[col_name] + 12.3
            expected_error = f"column {col_name!r} already exists and has dependents."
            with pytest.raises(excs.Error, match=re.compile(expected_error, re.IGNORECASE)):
                v.add_column(**{col_name: 'bbb'}, if_exists='replace')

    def test_from_dataframe(self, reset_db) -> None:
        t = self.create_tbl()

        # TODO(aaron-siegel): We actually do want to support this one
        with pytest.raises(excs.Error) as exc_info:
            pxt.create_view('test_view', t.select(t.c2))
        assert 'Cannot use `create_view` after `select`' in str(exc_info.value)

        with pytest.raises(excs.Error) as exc_info:
            pxt.create_view('test_view', t.group_by(t.c2))
        assert 'Cannot use `create_view` after `group_by`' in str(exc_info.value)

        with pytest.raises(excs.Error) as exc_info:
            pxt.create_view('test_view', t.order_by(t.c2))
        assert 'Cannot use `create_view` after `order_by`' in str(exc_info.value)

        with pytest.raises(excs.Error) as exc_info:
            pxt.create_view('test_view', t.limit(10))
        assert 'Cannot use `create_view` after `limit`' in str(exc_info.value)

    def test_parallel_views(self, reset_db) -> None:
        """Two views over the same base table, with non-overlapping filters"""
        t = self.create_tbl()

        # create view with filter and computed columns
        v1 = pxt.create_view('v1', t.where(t.c2 < 10), additional_columns={'v1': t.c3 * 2})
        # create another view with a non-overlapping filter and computed columns
        v2 = pxt.create_view('v2', t.where((t.c2 < 20) & (t.c2 >= 10)), additional_columns={'v1': t.c3 * 3})

        # sanity checks
        v1_query = v1.select(v1.v1).order_by(v1.c2)
        v2_query = v2.select(v2.v1).order_by(v2.c2)
        b1_query = t.select(t.c3 * 2).where(t.c2 < 10).order_by(t.c2)
        b2_query = t.select(t.c3 * 3).where((t.c2 >= 10) & (t.c2 < 20)).order_by(t.c2)
        assert_resultset_eq(v1_query.collect(), b1_query.collect())
        assert_resultset_eq(v2_query.collect(), b2_query.collect())

        # insert data: of 20 new rows, only 10 show up in each view
        rows = list(t.select(t.c1, t.c1n, t.c2, t.c3, t.c4, t.c5, t.c6, t.c7, t.c10).where(t.c2 < 20).collect())
        status = t.insert(rows)
        assert status.num_rows == 40
        assert t.count() == 120
        assert v1.count() == 20
        assert v2.count() == 20
        assert_resultset_eq(v1_query.collect(), b1_query.collect())
        assert_resultset_eq(v2_query.collect(), b2_query.collect())

        # update data: cascade to views
        status = t.update(
            {'c4': True, 'c3': t.c3 + 1, 'c10': t.c10 - 1.0}, where=(t.c2 >= 5) & (t.c2 < 15), cascade=True)
        assert status.num_rows == 20 * 2  # *2: rows affected in both base table and view
        assert t.count() == 120
        assert v1.count() == 20
        assert v2.count() == 20
        assert_resultset_eq(v1_query.collect(), b1_query.collect())
        assert_resultset_eq(v2_query.collect(), b2_query.collect())


        # base table delete is reflected in view
        status = t.delete(where=(t.c2 >= 5) & (t.c2 < 15))
        status.num_rows == 10 * 2  # *2: rows affected in both base table and view
        assert t.count() == 100
        assert v1.count() == 10
        assert v2.count() == 10
        assert_resultset_eq(v1_query.collect(), b1_query.collect())
        assert_resultset_eq(v2_query.collect(), b2_query.collect())

    def test_chained_views(self, reset_db) -> None:
        """Two views, the second one is a view over the first one"""
        t = self.create_tbl()

        # create view with filter and computed columns
        v1 = pxt.create_view('v1', t.where(t.c2 < 10), additional_columns={'col1': t.c3 * 2})
        # create a view on top of v1
        v2_schema = {
            'col2': t.c3 * 3,  # only base
            'col3': v1.col1 / 2,  # only v1
            'col4': t.c10 + v1.col1,  # both base and v1
        }
        v2 = pxt.create_view('v2', v1.where(t.c2 < 5), additional_columns=v2_schema)

        def check_views():
            assert_resultset_eq(
                v1.select(v1.col1).order_by(v1.c2).collect(),
                t.select(t.c3 * 2).where(t.c2 < 10).order_by(t.c2).collect())
            assert_resultset_eq(
                v2.select(v2.col1).order_by(v2.c2).collect(),
                v1.select(v1.col1).where(v1.c2 < 5).order_by(v1.c2).collect())
            assert_resultset_eq(
                v2.select(v2.col2).order_by(v2.c2).collect(),
                t.select(t.c3 * 3).where(t.c2 < 5).order_by(t.c2).collect())
            assert_resultset_eq(
                v2.select(v2.col3).order_by(v2.c2).collect(),
                v1.select(v1.col1 / 2).where(v1.c2 < 5).order_by(v2.c2).collect())
            assert_resultset_eq(
                v2.select(v2.col4).order_by(v2.c2).collect(),
                v1.select(v1.c10 + v1.col1).where(v1.c2 < 5).order_by(v1.c2).collect())
                #t.select(t.c10 * 2).where(t.c2 < 5).order_by(t.c2).collect())
        check_views()

        # insert data: of 20 new rows; 10 show up in v1, 5 in v2
        base_version, v1_version, v2_version = t._version, v1._version, v2._version
        rows = list(t.select(t.c1, t.c1n, t.c2, t.c3, t.c4, t.c5, t.c6, t.c7, t.c10).where(t.c2 < 20).collect())
        status = t.insert(rows)
        assert status.num_rows == 20 + 10 + 5
        assert t.count() == 120
        assert v1.count() == 20
        assert v2.count() == 10
        # all versions were incremented
        assert t._version == base_version + 1
        assert v1._version == v1_version + 1
        assert v2._version == v2_version + 1
        check_views()

        # update data: cascade to both views
        base_version, v1_version, v2_version = t._version, v1._version, v2._version
        status = t.update({'c4': True, 'c3': t.c3 + 1}, where=t.c2 < 15, cascade=True)
        assert status.num_rows == 30 + 20 + 10
        assert t.count() == 120
        # all versions were incremented
        assert t._version == base_version + 1
        assert v1._version == v1_version + 1
        assert v2._version == v2_version + 1
        check_views()

        # update data: cascade only to v2
        base_version, v1_version, v2_version = t._version, v1._version, v2._version
        status = t.update({'c10': t.c10 - 1.0}, where=t.c2 < 15, cascade=True)
        assert status.num_rows == 30 + 10
        assert t.count() == 120
        # v1 did not get updated
        assert t._version == base_version + 1
        assert v1._version == v1_version
        assert v2._version == v2_version + 1
        check_views()

        # base table delete is reflected in both views
        base_version, v1_version, v2_version = t._version, v1._version, v2._version
        status = t.delete(where=t.c2 == 0)
        status.num_rows == 1 + 1 + 1
        assert t.count() == 118
        assert v1.count() == 18
        assert v2.count() == 8
        # all versions were incremented
        assert t._version == base_version + 1
        assert v1._version == v1_version + 1
        assert v2._version == v2_version + 1
        check_views()

        # base table delete is reflected only in v1
        base_version, v1_version, v2_version = t._version, v1._version, v2._version
        status = t.delete(where=t.c2 == 5)
        status.num_rows == 1 + 1
        assert t.count() == 116
        assert v1.count() == 16
        assert v2.count() == 8
        # v2 was not updated
        assert t._version == base_version + 1
        assert v1._version == v1_version + 1
        assert v2._version == v2_version
        check_views()

    def test_unstored_columns(self, reset_db) -> None:
        """Test chained views with unstored columns"""
        # create table with image column and two updateable int columns
        schema = {'img': pxt.Image, 'int1': pxt.Int, 'int2': pxt.Int}
        t = pxt.create_table('test_tbl', schema)
        # populate table with images of a defined size
        width, height = 100, 100
        rows = [
            {
                'img': PIL.Image.new('RGB', (width, height), color=(0, 0, 0)).tobytes('jpeg', 'RGB'),
                'int1': i,
                'int2': i,
            }
            for i in range(100)
        ]
        t.insert(rows)

        # view with unstored column that depends on int1 and a manually updated column (int4)
        v1_schema = {
            'img2': {
                'value': t.img.crop([t.int1, t.int1, width, height]),
                'stored': False,
            },
            'int3': t.int1 * 2,
            'int4': pxt.Int,  # TODO: add default
        }
        logger.debug('******************* CREATE V1')
        v1 = pxt.create_view('v1', t, additional_columns=v1_schema)
        v1.update({'int4': 1})
        _ = v1.select(v1.img2.width, v1.img2.height).collect()

        # view with stored column that depends on t and view1
        v2_schema = {
            'img3': {
                # use the actual width and height of the image (not 100, which will pad the image)
                'value': v1.img2.crop([t.int1 + t.int2, v1.int3 + v1.int4, v1.img2.width, v1.img2.height]),
                'stored': True,
              },
        }
        logger.debug('******************* CREATE V2')
        v2 = pxt.create_view('v2', v1.where(v1.int1 < 10), additional_columns=v2_schema)

        def check_views() -> None:
            assert_resultset_eq(
                v1.select(v1.img2.width, v1.img2.height).order_by(v1.int1).collect(),
                t.select(t.img.width - t.int1, t.img.height - t.int1).order_by(t.int1).collect())
            assert_resultset_eq(
                v2.select(v2.img3.width, v2.img3.height).order_by(v2.int1).collect(),
                v1.select(v1.img2.width - v1.int1 - v1.int2, v1.img2.height - v1.int3 - v1.int4)\
                    .where(v1.int1 < 10).order_by(v1.int1).collect())
        check_views()

        logger.debug('******************* INSERT')
        status = t.insert(rows, on_error='ignore')
        v1.update({'int4': 1}, where=v1.int4 == None)
        logger.debug('******************* POST INSERT')
        check_views()

        # update int1:
        # - cascades to v1 and v2
        # - removes a row from v2 (only 9 rows in t now qualify)
        logger.debug('******************* UPDATE INT1')
        t.update({'int1': t.int1 + 1})
        logger.debug('******************* POST UPDATE INT1')
        check_views()

        # update int2:
        # - cascades only to v2
        # - but requires join against v1 to access int4
        # TODO: but requires join against v1 to access int3 and int4
        logger.debug('******************* UPDATE INT2')
        t.update({'int2': t.int2 + 1})
        logger.debug('******************* POST UPDATE INT2')
        check_views()

    def test_computed_cols(self, reset_db) -> None:
        t = self.create_tbl()

        # create view with computed columns
        schema = {
            'v1': t.c3 * 2.0,
            'v2': t.c6.f5,
        }
        v = pxt.create_view('test_view', t, additional_columns=schema)
        assert_resultset_eq(
            v.select(v.v1).order_by(v.c2).collect(),
            t.select(t.c3 * 2.0).order_by(t.c2).collect())
        # computed columns that don't reference the base table
        v.add_column(v3=v.v1 * 2.0)
        v.add_column(v4=v.v2[0])

        # use view md after reload
        reload_catalog()
        t = pxt.get_table('test_tbl')
        v = pxt.get_table('test_view')

        # insert data
        rows = list(t.select(t.c1, t.c1n, t.c2, t.c3, t.c4, t.c5, t.c6, t.c7, t.c10).collect())
        t.insert(rows)
        assert t.count() == 200
        assert_resultset_eq(
            v.select(v.v1).order_by(v.c2).collect(),
            t.select(t.c3 * 2.0).order_by(t.c2).collect())

        # update data: cascade to view
        t.update({'c4': True, 'c3': t.c3 + 1.0, 'c10': t.c10 - 1.0}, where=t.c2 < 5, cascade=True)
        assert t.count() == 200
        assert_resultset_eq(
            v.select(v.v1).order_by(v.c2).collect(),
            t.select(t.c3 * 2.0).order_by(t.c2).collect())

        # base table delete is reflected in view
        t.delete(where=t.c2 < 5)
        assert t.count() == 190
        assert_resultset_eq(
            v.select(v.v1).order_by(v.c2).collect(),
            t.select(t.c3 * 2.0).order_by(t.c2).collect())

    def test_filter(self, reset_db) -> None:
        t = create_test_tbl()

        # create view with filter
        v = pxt.create_view('test_view', t.where(t.c2 < 10))
        assert_resultset_eq(
            v.order_by(v.c2).collect(),
            t.where(t.c2 < 10).order_by(t.c2).collect())

        # use view md after reload
        reload_catalog()
        t = pxt.get_table('test_tbl')
        v = pxt.get_table('test_view')

        # insert data: of 20 new rows, only 10 are reflected in the view
        rows = list(t.select(t.c1, t.c1n, t.c2, t.c3, t.c4, t.c5, t.c6, t.c7).where(t.c2 < 20).collect())
        t.insert(rows)
        assert t.count() == 120
        assert_resultset_eq(
            v.order_by(v.c2).collect(),
            t.where(t.c2 < 10).order_by(t.c2).collect())

        # update data
        t.update({'c4': True, 'c3': t.c3 + 1.0}, where=t.c2 < 5, cascade=True)
        assert t.count() == 120
        assert_resultset_eq(
            v.order_by(v.c2).collect(),
            t.where(t.c2 < 10).order_by(t.c2).collect())

        # base table delete is reflected in view
        t.delete(where=t.c2 < 5)
        assert t.count() == 110
        assert_resultset_eq(
            v.order_by(v.c2).collect(),
            t.where(t.c2 < 10).order_by(t.c2).collect())

        # create view with filter containing datetime
        _ = pxt.create_view('test_view_2', t.where(t.c5 < datetime.datetime.now()))

    def test_view_of_snapshot(self, reset_db) -> None:
        """Test view over a snapshot"""
        t = self.create_tbl()
        snap = pxt.create_snapshot('test_snap', t)

        # create view with filter and computed columns
        schema = {
            'v1': snap.c3 * 2.0,
            'v2': snap.c6.f5,
        }
        v = pxt.create_view('test_view', snap.where(snap.c2 < 10), additional_columns=schema)

        def check_view(s: pxt.Table, v: pxt.Table) -> None:
            assert v.count() == s.where(s.c2 < 10).count()
            assert_resultset_eq(
                v.select(v.v1).order_by(v.c2).collect(),
                s.select(s.c3 * 2.0).where(s.c2 < 10).order_by(s.c2).collect())
            assert_resultset_eq(
                v.select(v.v2).order_by(v.c2).collect(),
                s.select(s.c6.f5).where(s.c2 < 10).order_by(s.c2).collect())

        check_view(snap, v)
        # computed columns that don't reference the base table
        v.add_column(v3=v.v1 * 2.0)
        v.add_column(v4=v.v2[0])
        assert v.count() == t.where(t.c2 < 10).count()

        # use view md after reload
        reload_catalog()
        t = pxt.get_table('test_tbl')
        snap = pxt.get_table('test_snap')
        v = pxt.get_table('test_view')

        # insert data: no changes to view
        rows = list(t.select(t.c1, t.c1n, t.c2, t.c3, t.c4, t.c5, t.c6, t.c7, t.c10).where(t.c2 < 20).collect())
        t.insert(rows)
        assert t.count() == 120
        check_view(snap, v)

        # update data: no changes to view
        t.update({'c4': True, 'c3': t.c3 + 1.0, 'c10': t.c10 - 1.0}, where=t.c2 < 5, cascade=True)
        assert t.count() == 120
        check_view(snap, v)

        # base table delete: no change to view
        t.delete(where=t.c2 < 5)
        assert t.count() == 110
        check_view(snap, v)

    def test_snapshots(self, reset_db) -> None:
        """Test snapshot of a view of a snapshot"""
        t = self.create_tbl()
        s = pxt.create_snapshot('test_snap', t)
        assert s.select(s.c2).order_by(s.c2).collect()['c2'] == t.select(t.c2).order_by(t.c2).collect()['c2']

        with pytest.raises(excs.Error) as exc_info:
            v = pxt.create_view('test_view', s, additional_columns={'v1': t.c3 * 2.0})
        assert 'value expression cannot be computed in the context of the base test_tbl' in str(exc_info.value)

        with pytest.raises(excs.Error) as exc_info:
            v = pxt.create_view('test_view', s.where(t.c2 < 10))
        assert 'filter cannot be computed in the context of the base test_tbl' in str(exc_info.value).lower()

        # create view with filter and computed columns
        schema = {
            'v1': s.c3 * 2.0,
            'v2': s.c6.f5,
        }
        v = pxt.create_view('test_view', s.where(s.c2 < 10), additional_columns=schema)
        orig_view_cols = v._schema.keys()
        view_s = pxt.create_snapshot('test_view_snap', v)
        assert set(view_s._schema.keys()) == set(orig_view_cols)

        def check(s1: pxt.Table, v: pxt.Table, s2: pxt.Table) -> None:
            assert s1.where(s1.c2 < 10).count() == v.count()
            assert v.count() == s2.count()
            assert_resultset_eq(
                s1.select(s1.c3 * 2.0, s1.c6.f5).where(s1.c2 < 10).order_by(s1.c2).collect(),
                v.select(v.v1, v.v2).order_by(v.c2).collect())
            assert_resultset_eq(
                v.select(v.c3, v.c6, v.v1, v.v2).order_by(v.c2).collect(),
                s2.select(s2.c3, s2.c6, s2.v1, s2.v2).order_by(s2.c2).collect())
        check(s, v, view_s)

        # add more columns
        v.add_column(v3=v.v1 * 2.0)
        v.add_column(v4=v.v2[0])
        check(s, v, view_s)
        assert set(view_s._schema.keys()) == set(orig_view_cols)

        # check md after reload
        reload_catalog()
        t = pxt.get_table('test_tbl')
        view_s = pxt.get_table('test_view_snap')
        check(s, v, view_s)
        assert set(view_s._schema.keys()) == set(orig_view_cols)

        # insert data: no changes to snapshot
        rows = list(t.select(t.c1, t.c1n, t.c2, t.c3, t.c4, t.c5, t.c6, t.c7, t.c10).where(t.c2 < 20).collect())
        t.insert(rows)
        assert t.count() == 120
        check(s, v, view_s)

        # update data: no changes to snapshot
        t.update({'c4': True, 'c3': t.c3 + 1.0, 'c10': t.c10 - 1.0}, where=t.c2 < 5, cascade=True)
        assert t.count() == 120
        check(s, v, view_s)

        # base table delete: no changes to snapshot
        t.delete(where=t.c2 < 5)
        assert t.count() == 110
        check(s, v, view_s)

    def test_column_defaults(self, reset_db) -> None:
        """
        Test that during insert() manually-supplied columns are materialized with their defaults and can be referenced
        in computed columns.
        """
        # TODO: use non-None default values once we have them
        t = pxt.create_table('table_1', {'id': pxt.Int, 'json_0': pxt.Json})
        # computed column depends on nullable non-computed column json_0
        t.add_column(computed_0=t.json_0.a)
        validate_update_status(t.insert(id=0, json_0={'a': 'b'}), expected_rows=1)
        assert t.where(t.computed_0 == None).count() == 0

        v = pxt.create_view('view_1', t.where(t.id >= 0), additional_columns={'json_1': pxt.Json})
        # computed column depends on nullable non-computed column json_1
        validate_update_status(v.add_column(computed_1=v.json_1.a))
        assert v.where(v.computed_1 == None).count() == 1
        validate_update_status(v.update({'json_1': {'a': 'b'}}), expected_rows=1)
        assert v.where(v.computed_1 == None).count() == 0

        # insert a new row with nulls in json_0/1
        validate_update_status(t.insert(id=1))
        # computed base table column for new row is null
        assert t.where(t.computed_0 == None).count() == 1
        # computed view column for new row is null
        assert v.where(v.computed_1 == None).count() == 1
