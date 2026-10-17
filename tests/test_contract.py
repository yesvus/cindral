import unittest

from cindral.contract import Contract, ContractError


VALID = """
timeout_minutes = 30

[image]
ref = "node:22-bookworm"

[env]
NODE_VERSION = "22"

[[services]]
name = "postgres"
image = "pgvector/pgvector:pg16"
env = { POSTGRES_USER = "postgres", POSTGRES_PASSWORD = "postgres" }

[[steps]]
run = "pnpm install --frozen-lockfile"

[[steps]]
run = "pnpm run ci"
"""


class ContractTest(unittest.TestCase):
    def test_parses_a_full_contract(self) -> None:
        contract = Contract.parse(VALID)
        self.assertEqual(contract.image, "node:22-bookworm")
        self.assertIsNone(contract.dockerfile)
        self.assertEqual(contract.timeout_minutes, 30)
        self.assertEqual(contract.env, {"NODE_VERSION": "22"})
        self.assertEqual(contract.steps, ("pnpm install --frozen-lockfile", "pnpm run ci"))
        self.assertEqual(len(contract.services), 1)
        self.assertEqual(contract.services[0].name, "postgres")
        self.assertEqual(contract.services[0].env["POSTGRES_USER"], "postgres")
        self.assertIsNone(contract.services[0].health_cmd)
        self.assertFalse(contract.docker)

    def test_parses_runner_docker_and_service_health(self) -> None:
        contract = Contract.parse(
            '[image]\nref = "x"\n\n[runner]\ndocker = true\n\n'
            '[[services]]\nname = "postgres"\nimage = "pgvector/pgvector:pg16"\n'
            'health_cmd = "pg_isready -U postgres"\n\n[[steps]]\nrun = "test"\n'
        )
        self.assertTrue(contract.docker)
        self.assertEqual(contract.services[0].health_cmd, "pg_isready -U postgres")

    def test_rejects_a_non_boolean_runner_docker(self) -> None:
        with self.assertRaises(ContractError):
            Contract.parse('[image]\nref = "x"\n\n[runner]\ndocker = "yes"\n\n[[steps]]\nrun = "test"\n')

    def test_rejects_an_empty_health_cmd(self) -> None:
        with self.assertRaises(ContractError):
            Contract.parse(
                '[image]\nref = "x"\n\n[[services]]\nname = "db"\nimage = "y"\n'
                'health_cmd = "  "\n\n[[steps]]\nrun = "test"\n'
            )

    def test_dockerfile_is_an_alternative_to_ref(self) -> None:
        contract = Contract.parse(
            """
[image]
dockerfile = ".cindral/Dockerfile"

[[steps]]
run = "make test"
"""
        )
        self.assertEqual(contract.dockerfile, ".cindral/Dockerfile")
        self.assertIsNone(contract.image)

    def test_requires_exactly_one_image_source(self) -> None:
        with self.assertRaises(ContractError):
            Contract.parse('[[steps]]\nrun = "test"\n')
        with self.assertRaises(ContractError):
            Contract.parse(
                '[image]\nref = "x"\ndockerfile = "Dockerfile"\n\n[[steps]]\nrun = "test"\n'
            )

    def test_requires_at_least_one_step(self) -> None:
        with self.assertRaises(ContractError):
            Contract.parse('[image]\nref = "x"\n')

    def test_rejects_an_escaping_dockerfile(self) -> None:
        with self.assertRaises(ContractError):
            Contract.parse(
                '[image]\ndockerfile = "../Dockerfile"\n\n[[steps]]\nrun = "test"\n'
            )

    def test_rejects_a_bad_service_name(self) -> None:
        with self.assertRaises(ContractError):
            Contract.parse(
                '[image]\nref = "x"\n\n[[services]]\nname = "-bad"\nimage = "y"\n\n[[steps]]\nrun = "test"\n'
            )

    def test_rejects_invalid_toml(self) -> None:
        with self.assertRaises(ContractError):
            Contract.parse("this is not toml = =")



class ContractAuthorityTableTest(unittest.TestCase):
    """[ci] contract_only is how a repository claims its CI, so it is validated."""

    BASE = """
[image]
ref = "python:3.12-bookworm"

[[steps]]
run = "echo one"
"""

    def parse(self, extra: str = ""):
        return Contract.parse(self.BASE + extra)

    def test_it_defaults_to_false(self) -> None:
        self.assertFalse(self.parse().contract_only)

    def test_it_reads_true(self) -> None:
        self.assertTrue(self.parse('\n[ci]\ncontract_only = true\n').contract_only)

    def test_it_reads_false(self) -> None:
        self.assertFalse(self.parse('\n[ci]\ncontract_only = false\n').contract_only)

    def test_a_non_boolean_is_rejected(self) -> None:
        # a string here would be truthy and silently invert the repository's
        # declared intent, which is the one outcome this key must not produce
        with self.assertRaises(ContractError) as caught:
            self.parse('\n[ci]\ncontract_only = "true"\n')
        self.assertIn("contract_only must be a boolean", str(caught.exception))

    def test_the_field_default_is_false_for_a_direct_construction(self) -> None:
        # parse always passes the value, so nothing else pins the default. It is
        # the one that decides whether an unconfigured contract claims authority,
        # and a wrong default here would invert a repository's intent silently.
        self.assertFalse(Contract(steps=("echo one",)).contract_only)

    def test_a_non_table_is_rejected(self) -> None:
        # before any table header, so it is a top-level key rather than a member
        # of [image], which is where a bare key lands if it is appended
        with self.assertRaises(ContractError) as caught:
            Contract.parse('ci = true\n' + self.BASE)
        self.assertIn("[ci] must be a table", str(caught.exception))


if __name__ == "__main__":
    unittest.main()


class ContractAuthorityReadTest(unittest.TestCase):
    """The guard reads one boolean, so the value has to be exactly that.

    `contract_only` decides whether a repository's committed workflows are read
    at all. A guard that accepts any truthy value lets `"false"` and `1` silently
    take a repository's CI over, which is the opposite of what the key says.
    """

    READER = "from cindral.inference import _contract_declares_itself_ci"
    BASE = """
[ci]
contract_only = %s

[image]
ref = "python:3.12-bookworm"

[[steps]]
run = "echo one"
"""

    def declares(self, value: str) -> bool:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ci.toml"
            path.write_text(self.BASE % value)
            from cindral.inference import _contract_declares_itself_ci

            return _contract_declares_itself_ci(path)

    def test_literal_true_declares(self) -> None:
        self.assertTrue(self.declares("true"))

    def test_a_quoted_false_does_not_declare(self) -> None:
        # the string "false" is truthy, and a repository that wrote it meant the
        # opposite of what the guard would then do
        self.assertFalse(self.declares('"false"'))

    def test_the_number_one_does_not_declare(self) -> None:
        self.assertFalse(self.declares("1"))

    def test_no_ci_section_does_not_declare(self) -> None:
        import tempfile
        from pathlib import Path

        from cindral.inference import _contract_declares_itself_ci

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ci.toml"
            path.write_text('[image]\nref = "python:3.12-bookworm"\n\n[[steps]]\nrun = "echo"\n')
            self.assertFalse(_contract_declares_itself_ci(path))

    def test_a_missing_file_does_not_declare(self) -> None:
        import tempfile
        from pathlib import Path

        from cindral.inference import _contract_declares_itself_ci

        with tempfile.TemporaryDirectory() as directory:
            self.assertFalse(_contract_declares_itself_ci(Path(directory) / "absent.toml"))


class GuardReadsOnceTest(unittest.TestCase):
    """The guard must not parse the whole contract to learn one boolean.

    A full parse here means the file is read twice on the push path, and worse,
    a contract missing a required section raises inside the guard. Answering
    False for that is correct, but it has to be an answer about the *key*, not
    about the whole document: a repository that wrote `[ci] contract_only = true`
    and a broken image section must still be told the file is broken, rather than
    quietly falling through to a workflow and running something else.
    """

    def test_the_guard_reads_the_key_rather_than_requiring_a_valid_contract(self) -> None:
        import tempfile
        from pathlib import Path

        from cindral.contract import ContractError
        from cindral.inference import _contract_declares_itself_ci

        # valid TOML, declares itself, and is missing the required [image]
        text = '[ci]\ncontract_only = true\n\n[[steps]]\nrun = "echo one"\n'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ci.toml"
            path.write_text(text)
            self.assertTrue(_contract_declares_itself_ci(path))
            # and the contract itself is still rejected when it is parsed
            with self.assertRaises(ContractError):
                from cindral.contract import Contract

                Contract.parse(text)
