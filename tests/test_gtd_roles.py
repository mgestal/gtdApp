import unittest

import gtd_roles


class ResolveRolesTests(unittest.TestCase):
    def test_defaults_without_config(self):
        roles = gtd_roles.resolve_roles({})
        self.assertEqual(roles["tags"]["next_action"], ["NextAction"])
        self.assertEqual(roles["folders"]["someday"], ["Sometime"])
        self.assertEqual(roles["folders"]["agenda"][0], "Agenda 🗃️")

    def test_override_accepts_string_list_and_commas(self):
        cfg = {"gtd_roles": {
            "tags": {"next_action": "Siguiente", "waiting": ["Esperando", "EnEspera"]},
            "folders": {"someday": "Algún día, Sometime"},
        }}
        roles = gtd_roles.resolve_roles(cfg)
        self.assertEqual(roles["tags"]["next_action"], ["Siguiente"])
        self.assertEqual(roles["tags"]["waiting"], ["Esperando", "EnEspera"])
        self.assertEqual(roles["folders"]["someday"], ["Algún día", "Sometime"])
        # Roles no configurados conservan el valor por defecto
        self.assertEqual(roles["tags"]["following"], ["EnSeguimiento"])

    def test_empty_or_invalid_values_fall_back_to_defaults(self):
        cfg = {"gtd_roles": {"tags": {"next_action": "  ,  ", "agenda": None}, "folders": "nope"}}
        roles = gtd_roles.resolve_roles(cfg)
        self.assertEqual(roles["tags"]["next_action"], ["NextAction"])
        self.assertEqual(roles["tags"]["agenda"], ["agenda"])
        self.assertEqual(roles["folders"]["adtv"], ["ADTV"])

    def test_parse_names_strips_at_and_dedupes_case_insensitive(self):
        self.assertEqual(gtd_roles.parse_names("@NextAction, nextaction, Otra"), ["NextAction", "Otra"])

    def test_sql_variants_include_unaccented(self):
        self.assertEqual(gtd_roles.sql_name_variants(["Periódica"]), ["periódica", "periodica"])



class AdminSaveRolesTests(unittest.TestCase):
    """Las tarjetas «Etiquetas GTD» y «Carpetas GTD» guardan cada una solo su parte."""

    CURRENT = {"gtd_roles": {"tags": {"waiting": ["EnEspera", "WaitingFor"]},
                             "folders": {"routines": ["♲ Rutinas", "Rutinas ♲"]}}}

    def save(self, data, current=None):
        import copy
        import os
        from unittest.mock import patch

        import app as application
        from gtd.views import admin as admin_views
        from tests.support import patch_all

        with patch.dict(os.environ, {"GTD_ADMIN_PASSWORD": "x"}), \
             application.app.test_request_context("/admin", method="POST", data=dict(action="save_gtd_roles", **data)), \
             patch_all("admin_required", return_value=True), \
             patch_all("load_config", return_value=copy.deepcopy(current if current is not None else self.CURRENT)), \
             patch_all("save_config") as save:
            response = admin_views.admin()
        return response, save

    def test_saving_tags_keeps_folders(self):
        response, save = self.save({"kind": "tags", "tag__next_action": "Siguiente, NextAction", "tag__waiting": ""})
        self.assertTrue(response.location.endswith("#gtd-role-tags"))
        self.assertEqual(save.call_args.args[0]["gtd_roles"], {
            "tags": {"next_action": ["Siguiente", "NextAction"]},
            "folders": {"routines": ["♲ Rutinas", "Rutinas ♲"]},
        })

    def test_saving_folders_keeps_tags(self):
        _response, save = self.save({"kind": "folders", "folder__someday": "Algún día"})
        self.assertEqual(save.call_args.args[0]["gtd_roles"], {
            "tags": {"waiting": ["EnEspera", "WaitingFor"]},
            "folders": {"someday": ["Algún día"]},
        })

    def test_all_defaults_removes_section(self):
        _response, save = self.save({"kind": "tags"}, current={"gtd_roles": {"tags": {"waiting": ["X"]}}})
        self.assertNotIn("gtd_roles", save.call_args.args[0])

    def test_unknown_kind_is_rejected(self):
        _response, save = self.save({"kind": "otra"})
        save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
