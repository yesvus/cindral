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


if __name__ == "__main__":
    unittest.main()
