# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
# isort:skip_file
import inspect
import time
import unittest
from collections import namedtuple
from unittest import mock
from unittest.mock import Mock, patch
from typing import Any

import jwt
import prison
import pytest

from flask import current_app

from superset.models.dashboard import Dashboard

from superset import app, appbuilder, db, security_manager, viz, ConnectorRegistry
from superset.connectors.druid.models import DruidCluster, DruidDatasource
from superset.connectors.sqla.models import SqlaTable
from superset.errors import ErrorLevel, SupersetError, SupersetErrorType
from superset.exceptions import SupersetSecurityException
from superset.models.core import Database
from superset.models.slice import Slice
from superset.sql_parse import Table
from superset.utils.core import (
    backend,
    get_example_default_schema,
)
from superset.utils.database import get_example_database
from superset.utils.urls import get_url_host
from superset.views.access_requests import AccessRequestsModelView

from .base_tests import SupersetTestCase
from tests.integration_tests.fixtures.public_role import (
    public_role_like_gamma,
    public_role_like_test_role,
)
from tests.integration_tests.fixtures.birth_names_dashboard import (
    load_birth_names_dashboard_with_slices,
    load_birth_names_data,
)
from tests.integration_tests.fixtures.world_bank_dashboard import (
    load_world_bank_dashboard_with_slices,
    load_world_bank_data,
)

NEW_SECURITY_CONVERGE_VIEWS = (
    "Annotation",
    "Database",
    "Dataset",
    "Dashboard",
    "CssTemplate",
    "Chart",
    "Query",
    "SavedQuery",
)


def get_perm_tuples(role_name):
    perm_set = set()
    for perm in security_manager.find_role(role_name).permissions:
        perm_set.add((perm.permission.name, perm.view_menu.name))
    return perm_set


SCHEMA_ACCESS_ROLE = "schema_access_role"


def create_schema_perm(view_menu_name: str) -> None:
    permission = "schema_access"
    security_manager.add_permission_view_menu(permission, view_menu_name)
    perm_view = security_manager.find_permission_view_menu(permission, view_menu_name)
    security_manager.add_permission_role(
        security_manager.find_role(SCHEMA_ACCESS_ROLE), perm_view
    )
    return None


def delete_schema_perm(view_menu_name: str) -> None:
    pv = security_manager.find_permission_view_menu("schema_access", "[examples].[2]")
    security_manager.del_permission_role(
        security_manager.find_role(SCHEMA_ACCESS_ROLE), pv
    )
    security_manager.del_permission_view_menu("schema_access", "[examples].[2]")
    return None


class TestRolePermission(SupersetTestCase):
    """Testing export role permissions."""

    def setUp(self):
        schema = get_example_default_schema()
        session = db.session
        security_manager.add_role(SCHEMA_ACCESS_ROLE)
        session.commit()

        ds = (
            db.session.query(SqlaTable)
            .filter_by(table_name="wb_health_population", schema=schema)
            .first()
        )
        ds.schema = "temp_schema"
        ds.schema_perm = ds.get_schema_perm()

        ds_slices = (
            session.query(Slice)
            .filter_by(datasource_type="table")
            .filter_by(datasource_id=ds.id)
            .all()
        )
        for s in ds_slices:
            s.schema_perm = ds.schema_perm
        create_schema_perm("[examples].[temp_schema]")
        gamma_user = security_manager.find_user(username="gamma")
        gamma_user.roles.append(security_manager.find_role(SCHEMA_ACCESS_ROLE))
        session.commit()

    def tearDown(self):
        session = db.session
        ds = (
            session.query(SqlaTable)
            .filter_by(table_name="wb_health_population", schema="temp_schema")
            .first()
        )
        schema_perm = ds.schema_perm
        ds.schema = get_example_default_schema()
        ds.schema_perm = None
        ds_slices = (
            session.query(Slice)
            .filter_by(datasource_type="table")
            .filter_by(datasource_id=ds.id)
            .all()
        )
        for s in ds_slices:
            s.schema_perm = None

        delete_schema_perm(schema_perm)
        session.delete(security_manager.find_role(SCHEMA_ACCESS_ROLE))
        session.commit()

    def test_set_perm_sqla_table(self):
        session = db.session
        table = SqlaTable(
            schema="tmp_schema",
            table_name="tmp_perm_table",
            database=get_example_database(),
        )
        session.add(table)
        session.commit()

        stored_table = (
            session.query(SqlaTable).filter_by(table_name="tmp_perm_table").one()
        )
        self.assertEqual(
            stored_table.perm, f"[examples].[tmp_perm_table](id:{stored_table.id})"
        )
        self.assertIsNotNone(
            security_manager.find_permission_view_menu(
                "datasource_access", stored_table.perm
            )
        )
        self.assertEqual(stored_table.schema_perm, "[examples].[tmp_schema]")
        self.assertIsNotNone(
            security_manager.find_permission_view_menu(
                "schema_access", stored_table.schema_perm
            )
        )

        # table name change
        stored_table.table_name = "tmp_perm_table_v2"
        session.commit()
        stored_table = (
            session.query(SqlaTable).filter_by(table_name="tmp_perm_table_v2").one()
        )
        self.assertEqual(
            stored_table.perm, f"[examples].[tmp_perm_table_v2](id:{stored_table.id})"
        )
        self.assertIsNotNone(
            security_manager.find_permission_view_menu(
                "datasource_access", stored_table.perm
            )
        )
        # no changes in schema
        self.assertEqual(stored_table.schema_perm, "[examples].[tmp_schema]")
        self.assertIsNotNone(
            security_manager.find_permission_view_menu(
                "schema_access", stored_table.schema_perm
            )
        )

        # schema name change
        stored_table.schema = "tmp_schema_v2"
        session.commit()
        stored_table = (
            session.query(SqlaTable).filter_by(table_name="tmp_perm_table_v2").one()
        )
        self.assertEqual(
            stored_table.perm, f"[examples].[tmp_perm_table_v2](id:{stored_table.id})"
        )
        self.assertIsNotNone(
            security_manager.find_permission_view_menu(
                "datasource_access", stored_table.perm
            )
        )
        # no changes in schema
        self.assertEqual(stored_table.schema_perm, "[examples].[tmp_schema_v2]")
        self.assertIsNotNone(
            security_manager.find_permission_view_menu(
                "schema_access", stored_table.schema_perm
            )
        )

        # database change
        new_db = Database(sqlalchemy_uri="sqlite://", database_name="tmp_db")
        session.add(new_db)
        stored_table.database = (
            session.query(Database).filter_by(database_name="tmp_db").one()
        )
        session.commit()
        stored_table = (
            session.query(SqlaTable).filter_by(table_name="tmp_perm_table_v2").one()
        )
        self.assertEqual(
            stored_table.perm, f"[tmp_db].[tmp_perm_table_v2](id:{stored_table.id})"
        )
        self.assertIsNotNone(
            security_manager.find_permission_view_menu(
                "datasource_access", stored_table.perm
            )
        )
        # no changes in schema
        self.assertEqual(stored_table.schema_perm, "[tmp_db].[tmp_schema_v2]")
        self.assertIsNotNone(
            security_manager.find_permission_view_menu(
                "schema_access", stored_table.schema_perm
            )
        )

        # no schema
        stored_table.schema = None
        session.commit()
        stored_table = (
            session.query(SqlaTable).filter_by(table_name="tmp_perm_table_v2").one()
        )
        self.assertEqual(
            stored_table.perm, f"[tmp_db].[tmp_perm_table_v2](id:{stored_table.id})"
        )
        self.assertIsNotNone(
            security_manager.find_permission_view_menu(
                "datasource_access", stored_table.perm
            )
        )
        self.assertIsNone(stored_table.schema_perm)

        session.delete(new_db)
        session.delete(stored_table)
        session.commit()

    @pytest.mark.usefixtures("load_world_bank_dashboard_with_slices")
    def test_set_perm_druid_datasource(self):
        self.create_druid_test_objects()
        session = db.session
        druid_cluster = (
            session.query(DruidCluster).filter_by(cluster_name="druid_test").one()
        )
        datasource = DruidDatasource(
            datasource_name="tmp_datasource",
            cluster=druid_cluster,
            cluster_id=druid_cluster.id,
        )
        session.add(datasource)
        session.commit()

        # store without a schema
        stored_datasource = (
            session.query(DruidDatasource)
            .filter_by(datasource_name="tmp_datasource")
            .one()
        )
        self.assertEqual(
            stored_datasource.perm,
            f"[druid_test].[tmp_datasource](id:{stored_datasource.id})",
        )
        self.assertIsNotNone(
            security_manager.find_permission_view_menu(
                "datasource_access", stored_datasource.perm
            )
        )
        self.assertIsNone(stored_datasource.schema_perm)

        # store with a schema
        stored_datasource.datasource_name = "tmp_schema.tmp_datasource"
        session.commit()
        self.assertEqual(
            stored_datasource.perm,
            f"[druid_test].[tmp_schema.tmp_datasource](id:{stored_datasource.id})",
        )
        self.assertIsNotNone(
            security_manager.find_permission_view_menu(
                "datasource_access", stored_datasource.perm
            )
        )
        self.assertIsNotNone(stored_datasource.schema_perm, "[druid_test].[tmp_schema]")
        self.assertIsNotNone(
            security_manager.find_permission_view_menu(
                "schema_access", stored_datasource.schema_perm
            )
        )

        session.delete(stored_datasource)
        session.commit()

    def test_set_perm_druid_cluster(self):
        session = db.session
        cluster = DruidCluster(cluster_name="tmp_druid_cluster")
        session.add(cluster)

        stored_cluster = (
            session.query(DruidCluster)
            .filter_by(cluster_name="tmp_druid_cluster")
            .one()
        )
        self.assertEqual(
            stored_cluster.perm, f"[tmp_druid_cluster].(id:{stored_cluster.id})"
        )
        self.assertIsNotNone(
            security_manager.find_permission_view_menu(
                "database_access", stored_cluster.perm
            )
        )

        stored_cluster.cluster_name = "tmp_druid_cluster2"
        session.commit()
        self.assertEqual(
            stored_cluster.perm, f"[tmp_druid_cluster2].(id:{stored_cluster.id})"
        )
        self.assertIsNotNone(
            security_manager.find_permission_view_menu(
                "database_access", stored_cluster.perm
            )
        )

        session.delete(stored_cluster)
        session.commit()

    def test_set_perm_database(self):
        session = db.session
        database = Database(database_name="tmp_database", sqlalchemy_uri="sqlite://")
        session.add(database)

        stored_db = (
            session.query(Database).filter_by(database_name="tmp_database").one()
        )
        self.assertEqual(stored_db.perm, f"[tmp_database].(id:{stored_db.id})")
        self.assertIsNotNone(
            security_manager.find_permission_view_menu(
                "database_access", stored_db.perm
            )
        )

        stored_db.database_name = "tmp_database2"
        session.commit()
        stored_db = (
            session.query(Database).filter_by(database_name="tmp_database2").one()
        )
        self.assertEqual(stored_db.perm, f"[tmp_database2].(id:{stored_db.id})")
        self.assertIsNotNone(
            security_manager.find_permission_view_menu(
                "database_access", stored_db.perm
            )
        )

        session.delete(stored_db)
        session.commit()

    def test_hybrid_perm_druid_cluster(self):
        cluster = DruidCluster(cluster_name="tmp_druid_cluster3")
        db.session.add(cluster)

        id_ = (
            db.session.query(DruidCluster.id)
            .filter_by(cluster_name="tmp_druid_cluster3")
            .scalar()
        )

        record = (
            db.session.query(DruidCluster)
            .filter_by(perm=f"[tmp_druid_cluster3].(id:{id_})")
            .one()
        )

        self.assertEqual(record.get_perm(), record.perm)
        self.assertEqual(record.id, id_)
        self.assertEqual(record.cluster_name, "tmp_druid_cluster3")
        db.session.delete(cluster)
        db.session.commit()

    def test_hybrid_perm_database(self):
        database = Database(database_name="tmp_database3", sqlalchemy_uri="sqlite://")

        db.session.add(database)

        id_ = (
            db.session.query(Database.id)
            .filter_by(database_name="tmp_database3")
            .scalar()
        )

        record = (
            db.session.query(Database)
            .filter_by(perm=f"[tmp_database3].(id:{id_})")
            .one()
        )

        self.assertEqual(record.get_perm(), record.perm)
        self.assertEqual(record.id, id_)
        self.assertEqual(record.database_name, "tmp_database3")
        db.session.delete(database)
        db.session.commit()

    def test_set_perm_slice(self):
        session = db.session
        database = Database(database_name="tmp_database", sqlalchemy_uri="sqlite://")
        table = SqlaTable(table_name="tmp_perm_table", database=database)
        session.add(database)
        session.add(table)
        session.commit()

        # no schema permission
        slice = Slice(
            datasource_id=table.id,
            datasource_type="table",
            datasource_name="tmp_perm_table",
            slice_name="slice_name",
        )
        session.add(slice)
        session.commit()

        slice = session.query(Slice).filter_by(slice_name="slice_name").one()
        self.assertEqual(slice.perm, table.perm)
        self.assertEqual(slice.perm, f"[tmp_database].[tmp_perm_table](id:{table.id})")
        self.assertEqual(slice.schema_perm, table.schema_perm)
        self.assertIsNone(slice.schema_perm)

        table.schema = "tmp_perm_schema"
        table.table_name = "tmp_perm_table_v2"
        session.commit()
        # TODO(bogdan): modify slice permissions on the table update.
        self.assertNotEqual(slice.perm, table.perm)
        self.assertEqual(slice.perm, f"[tmp_database].[tmp_perm_table](id:{table.id})")
        self.assertEqual(
            table.perm, f"[tmp_database].[tmp_perm_table_v2](id:{table.id})"
        )
        # TODO(bogdan): modify slice schema permissions on the table update.
        self.assertNotEqual(slice.schema_perm, table.schema_perm)
        self.assertIsNone(slice.schema_perm)

        # updating slice refreshes the permissions
        slice.slice_name = "slice_name_v2"
        session.commit()
        self.assertEqual(slice.perm, table.perm)
        self.assertEqual(
            slice.perm, f"[tmp_database].[tmp_perm_table_v2](id:{table.id})"
        )
        self.assertEqual(slice.schema_perm, table.schema_perm)
        self.assertEqual(slice.schema_perm, "[tmp_database].[tmp_perm_schema]")

        session.delete(slice)
        session.delete(table)
        session.delete(database)

        session.commit()

        # TODO test slice permission

    @patch("superset.security.manager.g")
    def test_schemas_accessible_by_user_admin(self, mock_g):
        mock_g.user = security_manager.find_user("admin")
        with self.client.application.test_request_context():
            database = get_example_database()
            schemas = security_manager.get_schemas_accessible_by_user(
                database, ["1", "2", "3"]
            )
            self.assertEqual(schemas, ["1", "2", "3"])  # no changes

    @patch("superset.security.manager.g")
    def test_schemas_accessible_by_user_schema_access(self, mock_g):
        # User has schema access to the schema 1
        create_schema_perm("[examples].[1]")
        mock_g.user = security_manager.find_user("gamma")
        with self.client.application.test_request_context():
            database = get_example_database()
            schemas = security_manager.get_schemas_accessible_by_user(
                database, ["1", "2", "3"]
            )
            # temp_schema is not passed in the params
            self.assertEqual(schemas, ["1"])
        delete_schema_perm("[examples].[1]")

    @patch("superset.security.manager.g")
    def test_schemas_accessible_by_user_datasource_access(self, mock_g):
        # User has schema access to the datasource temp_schema.wb_health_population in examples DB.
        mock_g.user = security_manager.find_user("gamma")
        with self.client.application.test_request_context():
            database = get_example_database()
            schemas = security_manager.get_schemas_accessible_by_user(
                database, ["temp_schema", "2", "3"]
            )
            self.assertEqual(schemas, ["temp_schema"])

    @patch("superset.security.manager.g")
    def test_schemas_accessible_by_user_datasource_and_schema_access(self, mock_g):
        # User has schema access to the datasource temp_schema.wb_health_population in examples DB.
        create_schema_perm("[examples].[2]")
        mock_g.user = security_manager.find_user("gamma")
        with self.client.application.test_request_context():
            database = get_example_database()
            schemas = security_manager.get_schemas_accessible_by_user(
                database, ["temp_schema", "2", "3"]
            )
            self.assertEqual(schemas, ["temp_schema", "2"])
        vm = security_manager.find_permission_view_menu(
            "schema_access", "[examples].[2]"
        )
        self.assertIsNotNone(vm)
        delete_schema_perm("[examples].[2]")

    @pytest.mark.usefixtures("load_world_bank_dashboard_with_slices")
    def test_gamma_user_schema_access_to_dashboards(self):
        dash = db.session.query(Dashboard).filter_by(slug="world_health").first()
        dash.published = True
        db.session.commit()

        self.login(username="gamma")
        data = str(self.client.get("api/v1/dashboard/").data)
        self.assertIn("/superset/dashboard/world_health/", data)
        self.assertNotIn("/superset/dashboard/births/", data)

    def test_gamma_user_schema_access_to_tables(self):
        self.login(username="gamma")
        data = str(self.client.get("tablemodelview/list/").data)
        self.assertIn("wb_health_population", data)
        self.assertNotIn("birth_names", data)

    @pytest.mark.usefixtures("load_world_bank_dashboard_with_slices")
    def test_gamma_user_schema_access_to_charts(self):
        self.login(username="gamma")
        data = str(self.client.get("api/v1/chart/").data)
        self.assertIn(
            "Life Expectancy VS Rural %", data
        )  # wb_health_population slice, has access
        self.assertIn(
            "Parallel Coordinates", data
        )  # wb_health_population slice, has access
        self.assertNotIn("Girl Name Cloud", data)  # birth_names slice, no access

    @pytest.mark.usefixtures("load_birth_names_dashboard_with_slices")
    @pytest.mark.usefixtures("public_role_like_gamma")
    def test_public_sync_role_data_perms(self):
        """
        Security: Tests if the sync role method preserves data access permissions
        if they already exist on a public role.
        Also check that non data access permissions are removed
        """
        table = db.session.query(SqlaTable).filter_by(table_name="birth_names").one()
        self.grant_public_access_to_table(table)
        public_role = security_manager.get_public_role()
        unwanted_pvm = security_manager.find_permission_view_menu(
            "menu_access", "Security"
        )
        public_role.permissions.append(unwanted_pvm)
        db.session.commit()

        security_manager.sync_role_definitions()
        public_role = security_manager.get_public_role()
        public_role_resource_names = [
            permission.view_menu.name for permission in public_role.permissions
        ]

        assert table.get_perm() in public_role_resource_names
        assert "Security" not in public_role_resource_names

        # Cleanup
        self.revoke_public_access_to_table(table)

    @pytest.mark.usefixtures("public_role_like_test_role")
    def test_public_sync_role_builtin_perms(self):
        """
        Security: Tests public role creation based on a builtin role
        """
        public_role = security_manager.get_public_role()
        public_role_resource_names = [
            [permission.view_menu.name, permission.permission.name]
            for permission in public_role.permissions
        ]
        for pvm in current_app.config["FAB_ROLES"]["TestRole"]:
            assert pvm in public_role_resource_names

    def test_sqllab_gamma_user_schema_access_to_sqllab(self):
        session = db.session

        example_db = session.query(Database).filter_by(database_name="examples").one()
        example_db.expose_in_sqllab = True
        session.commit()

        arguments = {
            "keys": ["none"],
            "filters": [{"col": "expose_in_sqllab", "opr": "eq", "value": True}],
            "order_columns": "database_name",
            "order_direction": "asc",
            "page": 0,
            "page_size": -1,
        }
        NEW_FLASK_GET_SQL_DBS_REQUEST = f"/api/v1/database/?q={prison.dumps(arguments)}"
        self.login(username="gamma")
        databases_json = self.client.get(NEW_FLASK_GET_SQL_DBS_REQUEST).json
        self.assertEqual(databases_json["count"], 1)
        self.logout()

    def assert_can_read(self, view_menu, permissions_set):
        if view_menu in NEW_SECURITY_CONVERGE_VIEWS:
            self.assertIn(("can_read", view_menu), permissions_set)
        else:
            self.assertIn(("can_list", view_menu), permissions_set)

    def assert_can_write(self, view_menu, permissions_set):
        if view_menu in NEW_SECURITY_CONVERGE_VIEWS:
            self.assertIn(("can_write", view_menu), permissions_set)
        else:
            self.assertIn(("can_add", view_menu), permissions_set)
            self.assertIn(("can_delete", view_menu), permissions_set)
            self.assertIn(("can_edit", view_menu), permissions_set)

    def assert_cannot_write(self, view_menu, permissions_set):
        if view_menu in NEW_SECURITY_CONVERGE_VIEWS:
            self.assertNotIn(("can_write", view_menu), permissions_set)
        else:
            self.assertNotIn(("can_add", view_menu), permissions_set)
            self.assertNotIn(("can_delete", view_menu), permissions_set)
            self.assertNotIn(("can_edit", view_menu), permissions_set)
            self.assertNotIn(("can_save", view_menu), permissions_set)

    def assert_can_all(self, view_menu, permissions_set):
        self.assert_can_read(view_menu, permissions_set)
        self.assert_can_write(view_menu, permissions_set)

    def assert_can_menu(self, view_menu, permissions_set):
        self.assertIn(("menu_access", view_menu), permissions_set)

    def assert_can_gamma(self, perm_set):
        self.assert_can_read("Dataset", perm_set)

        # make sure that user can create slices and dashboards
        self.assert_can_all("Dashboard", perm_set)
        self.assert_can_all("Chart", perm_set)

        self.assertIn(("can_add_slices", "Superset"), perm_set)
        self.assertIn(("can_copy_dash", "Superset"), perm_set)
        self.assertIn(("can_created_dashboards", "Superset"), perm_set)
        self.assertIn(("can_created_slices", "Superset"), perm_set)
        self.assertIn(("can_csv", "Superset"), perm_set)
        self.assertIn(("can_dashboard", "Superset"), perm_set)
        self.assertIn(("can_explore", "Superset"), perm_set)
        self.assertIn(("can_share_chart", "Superset"), perm_set)
        self.assertIn(("can_share_dashboard", "Superset"), perm_set)
        self.assertIn(("can_explore_json", "Superset"), perm_set)
        self.assertIn(("can_fave_dashboards", "Superset"), perm_set)
        self.assertIn(("can_fave_slices", "Superset"), perm_set)
        self.assertIn(("can_save_dash", "Superset"), perm_set)
        self.assertIn(("can_slice", "Superset"), perm_set)
        self.assertIn(("can_explore_json", "Superset"), perm_set)
        self.assertIn(("can_userinfo", "UserDBModelView"), perm_set)
        self.assert_can_menu("Databases", perm_set)
        self.assert_can_menu("Datasets", perm_set)
        self.assert_can_menu("Data", perm_set)
        self.assert_can_menu("Charts", perm_set)
        self.assert_can_menu("Dashboards", perm_set)

    def assert_can_alpha(self, perm_set):
        self.assert_can_all("Annotation", perm_set)
        self.assert_can_all("CssTemplate", perm_set)
        self.assert_can_all("Dataset", perm_set)
        self.assert_can_read("Query", perm_set)
        self.assert_can_read("Database", perm_set)
        self.assertIn(("can_import_dashboards", "Superset"), perm_set)
        self.assertIn(("can_this_form_post", "CsvToDatabaseView"), perm_set)
        self.assertIn(("can_this_form_get", "CsvToDatabaseView"), perm_set)
        self.assert_can_menu("Manage", perm_set)
        self.assert_can_menu("Annotation Layers", perm_set)
        self.assert_can_menu("CSS Templates", perm_set)
        self.assertIn(("all_datasource_access", "all_datasource_access"), perm_set)

    def assert_cannot_alpha(self, perm_set):
        if app.config["ENABLE_ACCESS_REQUEST"]:
            self.assert_cannot_write("AccessRequestsModelView", perm_set)
            self.assert_can_all("AccessRequestsModelView", perm_set)
        self.assert_cannot_write("Queries", perm_set)
        self.assert_cannot_write("RoleModelView", perm_set)
        self.assert_cannot_write("UserDBModelView", perm_set)
        self.assert_cannot_write("Database", perm_set)

    def assert_can_admin(self, perm_set):
        self.assert_can_all("Database", perm_set)
        self.assert_can_all("RoleModelView", perm_set)
        self.assert_can_all("UserDBModelView", perm_set)

        self.assertIn(("all_database_access", "all_database_access"), perm_set)
        self.assertIn(("can_override_role_permissions", "Superset"), perm_set)
        self.assertIn(("can_sync_druid_source", "Superset"), perm_set)
        self.assertIn(("can_override_role_permissions", "Superset"), perm_set)
        self.assertIn(("can_approve", "Superset"), perm_set)

        self.assert_can_menu("Security", perm_set)
        self.assert_can_menu("List Users", perm_set)
        self.assert_can_menu("List Roles", perm_set)

    def test_is_admin_only(self):
        self.assertFalse(
            security_manager._is_admin_only(
                security_manager.find_permission_view_menu("can_read", "Dataset")
            )
        )
        self.assertFalse(
            security_manager._is_admin_only(
                security_manager.find_permission_view_menu(
                    "all_datasource_access", "all_datasource_access"
                )
            )
        )

        log_permissions = ["can_read"]
        for log_permission in log_permissions:
            self.assertTrue(
                security_manager._is_admin_only(
                    security_manager.find_permission_view_menu(log_permission, "Log")
                )
            )

        if app.config["ENABLE_ACCESS_REQUEST"]:
            self.assertTrue(
                security_manager._is_admin_only(
                    security_manager.find_permission_view_menu(
                        "can_list", "AccessRequestsModelView"
                    )
                )
            )
        self.assertTrue(
            security_manager._is_admin_only(
                security_manager.find_permission_view_menu(
                    "can_edit", "UserDBModelView"
                )
            )
        )
        self.assertTrue(
            security_manager._is_admin_only(
                security_manager.find_permission_view_menu("can_approve", "Superset")
            )
        )

    @unittest.skipUnless(
        SupersetTestCase.is_module_installed("pydruid"), "pydruid not installed"
    )
    def test_is_alpha_only(self):
        self.assertFalse(
            security_manager._is_alpha_only(
                security_manager.find_permission_view_menu("can_read", "Dataset")
            )
        )

        self.assertTrue(
            security_manager._is_alpha_only(
                security_manager.find_permission_view_menu("can_write", "Dataset")
            )
        )
        self.assertTrue(
            security_manager._is_alpha_only(
                security_manager.find_permission_view_menu(
                    "all_datasource_access", "all_datasource_access"
                )
            )
        )
        self.assertTrue(
            security_manager._is_alpha_only(
                security_manager.find_permission_view_menu(
                    "all_database_access", "all_database_access"
                )
            )
        )

    def test_is_gamma_pvm(self):
        self.assertTrue(
            security_manager._is_gamma_pvm(
                security_manager.find_permission_view_menu("can_read", "Dataset")
            )
        )

    def test_gamma_permissions_basic(self):
        self.assert_can_gamma(get_perm_tuples("Gamma"))
        self.assert_cannot_alpha(get_perm_tuples("Gamma"))

    @pytest.mark.usefixtures("public_role_like_gamma")
    def test_public_permissions_basic(self):
        self.assert_can_gamma(get_perm_tuples("Public"))

    @unittest.skipUnless(
        SupersetTestCase.is_module_installed("pydruid"), "pydruid not installed"
    )
    def test_alpha_permissions(self):
        alpha_perm_tuples = get_perm_tuples("Alpha")
        self.assert_can_gamma(alpha_perm_tuples)
        self.assert_can_alpha(alpha_perm_tuples)
        self.assert_cannot_alpha(alpha_perm_tuples)

    @pytest.mark.usefixtures("load_world_bank_dashboard_with_slices")
    def test_admin_permissions(self):
        if backend() == "hive":
            return

        self.assert_can_gamma(get_perm_tuples("Admin"))
        self.assert_can_alpha(get_perm_tuples("Admin"))
        self.assert_can_admin(get_perm_tuples("Admin"))

    def test_sql_lab_permissions(self):
        sql_lab_set = get_perm_tuples("sql_lab")
        self.assertIn(("can_csv", "Superset"), sql_lab_set)
        self.assertIn(("can_read", "Database"), sql_lab_set)
        self.assertIn(("can_read", "SavedQuery"), sql_lab_set)
        self.assertIn(("can_sql_json", "Superset"), sql_lab_set)
        self.assertIn(("can_sqllab_viz", "Superset"), sql_lab_set)
        self.assertIn(("can_sqllab_table_viz", "Superset"), sql_lab_set)
        self.assertIn(("can_sqllab", "Superset"), sql_lab_set)

        self.assertIn(("menu_access", "SQL Lab"), sql_lab_set)
        self.assertIn(("menu_access", "SQL Editor"), sql_lab_set)
        self.assertIn(("menu_access", "Saved Queries"), sql_lab_set)
        self.assertIn(("menu_access", "Query Search"), sql_lab_set)

        self.assert_cannot_alpha(sql_lab_set)

    def test_granter_permissions(self):
        granter_set = get_perm_tuples("granter")
        self.assertIn(("can_override_role_permissions", "Superset"), granter_set)
        self.assertIn(("can_approve", "Superset"), granter_set)

        self.assert_cannot_alpha(granter_set)

    def test_gamma_permissions(self):
        gamma_perm_set = set()
        for perm in security_manager.find_role("Gamma").permissions:
            gamma_perm_set.add((perm.permission.name, perm.view_menu.name))

        # check read only perms

        # make sure that user can create slices and dashboards
        self.assert_can_all("Dashboard", gamma_perm_set)
        self.assert_can_read("Dataset", gamma_perm_set)

        # make sure that user can create slices and dashboards
        self.assert_can_all("Chart", gamma_perm_set)

        self.assert_cannot_write("UserDBModelView", gamma_perm_set)
        self.assert_cannot_write("RoleModelView", gamma_perm_set)

        self.assertIn(("can_add_slices", "Superset"), gamma_perm_set)
        self.assertIn(("can_copy_dash", "Superset"), gamma_perm_set)
        self.assertIn(("can_created_dashboards", "Superset"), gamma_perm_set)
        self.assertIn(("can_created_slices", "Superset"), gamma_perm_set)
        self.assertIn(("can_csv", "Superset"), gamma_perm_set)
        self.assertIn(("can_dashboard", "Superset"), gamma_perm_set)
        self.assertIn(("can_explore", "Superset"), gamma_perm_set)
        self.assertIn(("can_share_chart", "Superset"), gamma_perm_set)
        self.assertIn(("can_share_dashboard", "Superset"), gamma_perm_set)
        self.assertIn(("can_explore_json", "Superset"), gamma_perm_set)
        self.assertIn(("can_fave_dashboards", "Superset"), gamma_perm_set)
        self.assertIn(("can_fave_slices", "Superset"), gamma_perm_set)
        self.assertIn(("can_save_dash", "Superset"), gamma_perm_set)
        self.assertIn(("can_slice", "Superset"), gamma_perm_set)
        self.assertIn(("can_userinfo", "UserDBModelView"), gamma_perm_set)

    def test_views_are_secured(self):
        """Preventing the addition of unsecured views without has_access decorator"""
        # These FAB views are secured in their body as opposed to by decorators
        method_allowlist = ("action", "action_post")
        # List of redirect & other benign views
        views_allowlist = [
            ["MyIndexView", "index"],
            ["UtilView", "back"],
            ["LocaleView", "index"],
            ["AuthDBView", "login"],
            ["AuthDBView", "logout"],
            ["CurrentUserRestApi", "me"],
            ["Dashboard", "embedded"],
            ["R", "index"],
            ["Superset", "log"],
            ["Superset", "theme"],
            ["Superset", "welcome"],
            ["SecurityApi", "login"],
            ["SecurityApi", "refresh"],
            ["SupersetIndexView", "index"],
        ]
        unsecured_views = []
        for view_class in appbuilder.baseviews:
            class_name = view_class.__class__.__name__
            for name, value in inspect.getmembers(
                view_class, predicate=inspect.ismethod
            ):
                if (
                    name not in method_allowlist
                    and [class_name, name] not in views_allowlist
                    and hasattr(value, "_urls")
                    and not hasattr(value, "_permission_name")
                ):
                    unsecured_views.append((class_name, name))
        if unsecured_views:
            view_str = "\n".join([str(v) for v in unsecured_views])
            raise Exception(f"Some views are not secured:\n{view_str}")


class TestSecurityManager(SupersetTestCase):
    """
    Testing the Security Manager.
    """

    @patch("superset.security.SupersetSecurityManager.raise_for_access")
    def test_can_access_datasource(self, mock_raise_for_access):
        datasource = self.get_datasource_mock()

        mock_raise_for_access.return_value = None
        self.assertTrue(security_manager.can_access_datasource(datasource=datasource))

        mock_raise_for_access.side_effect = SupersetSecurityException(
            SupersetError(
                "dummy",
                SupersetErrorType.DATASOURCE_SECURITY_ACCESS_ERROR,
                ErrorLevel.ERROR,
            )
        )

        self.assertFalse(security_manager.can_access_datasource(datasource=datasource))

    @patch("superset.security.SupersetSecurityManager.raise_for_access")
    def test_can_access_table(self, mock_raise_for_access):
        database = get_example_database()
        table = Table("bar", "foo")

        mock_raise_for_access.return_value = None
        self.assertTrue(security_manager.can_access_table(database, table))

        mock_raise_for_access.side_effect = SupersetSecurityException(
            SupersetError(
                "dummy", SupersetErrorType.TABLE_SECURITY_ACCESS_ERROR, ErrorLevel.ERROR
            )
        )

        self.assertFalse(security_manager.can_access_table(database, table))

    @patch("superset.security.SupersetSecurityManager.can_access")
    @patch("superset.security.SupersetSecurityManager.can_access_schema")
    def test_raise_for_access_datasource(self, mock_can_access_schema, mock_can_access):
        datasource = self.get_datasource_mock()

        mock_can_access_schema.return_value = True
        security_manager.raise_for_access(datasource=datasource)

        mock_can_access.return_value = False
        mock_can_access_schema.return_value = False

        with self.assertRaises(SupersetSecurityException):
            security_manager.raise_for_access(datasource=datasource)

    @patch("superset.security.SupersetSecurityManager.can_access")
    def test_raise_for_access_query(self, mock_can_access):
        query = Mock(
            database=get_example_database(), schema="bar", sql="SELECT * FROM foo"
        )

        mock_can_access.return_value = True
        security_manager.raise_for_access(query=query)

        mock_can_access.return_value = False

        with self.assertRaises(SupersetSecurityException):
            security_manager.raise_for_access(query=query)

    @patch("superset.security.SupersetSecurityManager.can_access")
    @patch("superset.security.SupersetSecurityManager.can_access_schema")
    def test_raise_for_access_query_context(
        self, mock_can_access_schema, mock_can_access
    ):
        query_context = Mock(datasource=self.get_datasource_mock())

        mock_can_access_schema.return_value = True
        security_manager.raise_for_access(query_context=query_context)

        mock_can_access.return_value = False
        mock_can_access_schema.return_value = False

        with self.assertRaises(SupersetSecurityException):
            security_manager.raise_for_access(query_context=query_context)

    @patch("superset.security.SupersetSecurityManager.can_access")
    def test_raise_for_access_table(self, mock_can_access):
        database = get_example_database()
        table = Table("bar", "foo")

        mock_can_access.return_value = True
        security_manager.raise_for_access(database=database, table=table)

        mock_can_access.return_value = False

        with self.assertRaises(SupersetSecurityException):
            security_manager.raise_for_access(database=database, table=table)

    @patch("superset.security.SupersetSecurityManager.can_access")
    @patch("superset.security.SupersetSecurityManager.can_access_schema")
    def test_raise_for_access_viz(self, mock_can_access_schema, mock_can_access):
        test_viz = viz.TableViz(self.get_datasource_mock(), form_data={})

        mock_can_access_schema.return_value = True
        security_manager.raise_for_access(viz=test_viz)

        mock_can_access.return_value = False
        mock_can_access_schema.return_value = False

        with self.assertRaises(SupersetSecurityException):
            security_manager.raise_for_access(viz=test_viz)

    @patch("superset.security.manager.g")
    def test_get_user_roles(self, mock_g):
        admin = security_manager.find_user("admin")
        mock_g.user = admin
        roles = security_manager.get_user_roles()
        self.assertEqual(admin.roles, roles)

    @patch("superset.security.manager.g")
    def test_get_anonymous_roles(self, mock_g):
        mock_g.user = security_manager.get_anonymous_user()
        roles = security_manager.get_user_roles()
        self.assertEqual([security_manager.get_public_role()], roles)


class TestAccessRequestEndpoints(SupersetTestCase):
    def test_access_request_disabled(self):
        with patch.object(AccessRequestsModelView, "is_enabled", return_value=False):
            self.login("admin")
            uri = "/accessrequestsmodelview/list/"
            rv = self.client.get(uri)
            self.assertEqual(rv.status_code, 404)

    def test_access_request_enabled(self):
        with patch.object(AccessRequestsModelView, "is_enabled", return_value=True):
            self.login("admin")
            uri = "/accessrequestsmodelview/list/"
            rv = self.client.get(uri)
            self.assertLess(rv.status_code, 400)


class TestDatasources(SupersetTestCase):
    @patch("superset.security.manager.g")
    @patch("superset.security.SupersetSecurityManager.can_access_database")
    @patch("superset.security.SupersetSecurityManager.get_session")
    def test_get_user_datasources_admin(
        self, mock_get_session, mock_can_access_database, mock_g
    ):
        Datasource = namedtuple("Datasource", ["database", "schema", "name"])
        mock_g.user = security_manager.find_user("admin")
        mock_can_access_database.return_value = True
        mock_get_session.query.return_value.filter.return_value.all.return_value = []

        with mock.patch.object(
            ConnectorRegistry, "get_all_datasources"
        ) as mock_get_all_datasources:
            mock_get_all_datasources.return_value = [
                Datasource("database1", "schema1", "table1"),
                Datasource("database1", "schema1", "table2"),
                Datasource("database2", None, "table1"),
            ]

            datasources = security_manager.get_user_datasources()

        assert sorted(datasources) == [
            Datasource("database1", "schema1", "table1"),
            Datasource("database1", "schema1", "table2"),
            Datasource("database2", None, "table1"),
        ]

    @patch("superset.security.manager.g")
    @patch("superset.security.SupersetSecurityManager.can_access_database")
    @patch("superset.security.SupersetSecurityManager.get_session")
    def test_get_user_datasources_gamma(
        self, mock_get_session, mock_can_access_database, mock_g
    ):
        Datasource = namedtuple("Datasource", ["database", "schema", "name"])
        mock_g.user = security_manager.find_user("gamma")
        mock_can_access_database.return_value = False
        mock_get_session.query.return_value.filter.return_value.all.return_value = []

        with mock.patch.object(
            ConnectorRegistry, "get_all_datasources"
        ) as mock_get_all_datasources:
            mock_get_all_datasources.return_value = [
                Datasource("database1", "schema1", "table1"),
                Datasource("database1", "schema1", "table2"),
                Datasource("database2", None, "table1"),
            ]

            datasources = security_manager.get_user_datasources()

        assert datasources == []

    @patch("superset.security.manager.g")
    @patch("superset.security.SupersetSecurityManager.can_access_database")
    @patch("superset.security.SupersetSecurityManager.get_session")
    def test_get_user_datasources_gamma_with_schema(
        self, mock_get_session, mock_can_access_database, mock_g
    ):
        Datasource = namedtuple("Datasource", ["database", "schema", "name"])
        mock_g.user = security_manager.find_user("gamma")
        mock_can_access_database.return_value = False

        mock_get_session.query.return_value.filter.return_value.all.return_value = [
            Datasource("database1", "schema1", "table1"),
            Datasource("database1", "schema1", "table2"),
        ]

        with mock.patch.object(
            ConnectorRegistry, "get_all_datasources"
        ) as mock_get_all_datasources:
            mock_get_all_datasources.return_value = [
                Datasource("database1", "schema1", "table1"),
                Datasource("database1", "schema1", "table2"),
                Datasource("database2", None, "table1"),
            ]

            datasources = security_manager.get_user_datasources()

        assert sorted(datasources) == [
            Datasource("database1", "schema1", "table1"),
            Datasource("database1", "schema1", "table2"),
        ]


class FakeRequest:
    headers: Any = {}


class TestGuestTokens(SupersetTestCase):
    def create_guest_token(self):
        user = {"username": "test_guest"}
        resources = [{"some": "resource"}]
        rls = [{"dataset": 1, "clause": "access = 1"}]
        return security_manager.create_guest_access_token(user, resources, rls)

    @patch("superset.security.SupersetSecurityManager._get_current_epoch_time")
    def test_create_guest_access_token(self, get_time_mock):
        now = time.time()
        get_time_mock.return_value = now  # so we know what it should =

        user = {"username": "test_guest"}
        resources = [{"some": "resource"}]
        rls = [{"dataset": 1, "clause": "access = 1"}]
        token = security_manager.create_guest_access_token(user, resources, rls)
        aud = get_url_host()
        # unfortunately we cannot mock time in the jwt lib
        decoded_token = jwt.decode(
            token,
            self.app.config["GUEST_TOKEN_JWT_SECRET"],
            algorithms=[self.app.config["GUEST_TOKEN_JWT_ALGO"]],
            audience=aud,
        )

        self.assertEqual(user, decoded_token["user"])
        self.assertEqual(resources, decoded_token["resources"])
        self.assertEqual(now, decoded_token["iat"])
        self.assertEqual(aud, decoded_token["aud"])
        self.assertEqual("guest", decoded_token["type"])
        self.assertEqual(
            now + (self.app.config["GUEST_TOKEN_JWT_EXP_SECONDS"] * 1000),
            decoded_token["exp"],
        )

    def test_get_guest_user(self):
        token = self.create_guest_token()
        fake_request = FakeRequest()
        fake_request.headers[current_app.config["GUEST_TOKEN_HEADER_NAME"]] = token

        guest_user = security_manager.get_guest_user_from_request(fake_request)

        self.assertIsNotNone(guest_user)
        self.assertEqual("test_guest", guest_user.username)

    @patch("superset.security.SupersetSecurityManager._get_current_epoch_time")
    def test_get_guest_user_expired_token(self, get_time_mock):
        # make a just-expired token
        get_time_mock.return_value = (
            time.time() - (self.app.config["GUEST_TOKEN_JWT_EXP_SECONDS"] * 1000) - 1
        )
        token = self.create_guest_token()
        fake_request = FakeRequest()
        fake_request.headers[current_app.config["GUEST_TOKEN_HEADER_NAME"]] = token

        guest_user = security_manager.get_guest_user_from_request(fake_request)

        self.assertIsNone(guest_user)

    def test_get_guest_user_no_user(self):
        user = None
        resources = [{"type": "dashboard", "id": 1}]
        rls = {}
        token = security_manager.create_guest_access_token(user, resources, rls)
        fake_request = FakeRequest()
        fake_request.headers[current_app.config["GUEST_TOKEN_HEADER_NAME"]] = token
        guest_user = security_manager.get_guest_user_from_request(fake_request)

        self.assertIsNone(guest_user)
        self.assertRaisesRegex(ValueError, "Guest token does not contain a user claim")

    def test_get_guest_user_no_resource(self):
        user = {"username": "test_guest"}
        resources = []
        rls = {}
        token = security_manager.create_guest_access_token(user, resources, rls)
        fake_request = FakeRequest()
        fake_request.headers[current_app.config["GUEST_TOKEN_HEADER_NAME"]] = token
        security_manager.get_guest_user_from_request(fake_request)

        self.assertRaisesRegex(
            ValueError, "Guest token does not contain a resources claim"
        )

    def test_get_guest_user_not_guest_type(self):
        now = time.time()
        user = {"username": "test_guest"}
        resources = [{"some": "resource"}]
        aud = get_url_host()

        claims = {
            "user": user,
            "resources": resources,
            "rls_rules": [],
            # standard jwt claims:
            "aud": aud,
            "iat": now,  # issued at
            "type": "not_guest",
        }
        token = jwt.encode(
            claims,
            self.app.config["GUEST_TOKEN_JWT_SECRET"],
            algorithm=self.app.config["GUEST_TOKEN_JWT_ALGO"],
        )
        fake_request = FakeRequest()
        fake_request.headers[current_app.config["GUEST_TOKEN_HEADER_NAME"]] = token
        guest_user = security_manager.get_guest_user_from_request(fake_request)

        self.assertIsNone(guest_user)
        self.assertRaisesRegex(ValueError, "This is not a guest token.")

    def test_get_guest_user_bad_audience(self):
        now = time.time()
        user = {"username": "test_guest"}
        resources = [{"some": "resource"}]
        aud = get_url_host()

        claims = {
            "user": user,
            "resources": resources,
            "rls_rules": [],
            # standard jwt claims:
            "aud": "bad_audience",
            "iat": now,  # issued at
            "type": "guest",
        }
        token = jwt.encode(
            claims,
            self.app.config["GUEST_TOKEN_JWT_SECRET"],
            algorithm=self.app.config["GUEST_TOKEN_JWT_ALGO"],
        )
        fake_request = FakeRequest()
        fake_request.headers[current_app.config["GUEST_TOKEN_HEADER_NAME"]] = token
        guest_user = security_manager.get_guest_user_from_request(fake_request)

        self.assertRaisesRegex(jwt.exceptions.InvalidAudienceError, "Invalid audience")
        self.assertIsNone(guest_user)

    @patch("superset.security.SupersetSecurityManager._get_current_epoch_time")
    def test_create_guest_access_token_callable_audience(self, get_time_mock):
        now = time.time()
        get_time_mock.return_value = now
        app.config["GUEST_TOKEN_JWT_AUDIENCE"] = Mock(return_value="cool_code")

        user = {"username": "test_guest"}
        resources = [{"some": "resource"}]
        rls = [{"dataset": 1, "clause": "access = 1"}]
        token = security_manager.create_guest_access_token(user, resources, rls)

        decoded_token = jwt.decode(
            token,
            self.app.config["GUEST_TOKEN_JWT_SECRET"],
            algorithms=[self.app.config["GUEST_TOKEN_JWT_ALGO"]],
            audience="cool_code",
        )
        app.config["GUEST_TOKEN_JWT_AUDIENCE"].assert_called_once()
        self.assertEqual("cool_code", decoded_token["aud"])
        self.assertEqual("guest", decoded_token["type"])
        app.config["GUEST_TOKEN_JWT_AUDIENCE"] = None
