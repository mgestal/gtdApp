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


if __name__ == "__main__":
    unittest.main()
