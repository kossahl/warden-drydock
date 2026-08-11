import re
import tomllib
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
AGENT_DIRECTORY = REPOSITORY_ROOT / ".codex" / "agents"
AGENTS_PATH = REPOSITORY_ROOT / "AGENTS.md"

AGENTS = {
    "product_designer": {
        "owns": (
            "information architecture",
            "flows",
            "wireframes",
            "ui states",
            "responsive",
            "accessibility",
        ),
        "excludes": (
            "product scope",
            "architecture",
            "production code",
        ),
    },
    "hosted_backend_implementer": {
        "owns": (
            "hosted api",
            "service",
            "persistence",
            "revisions",
            "retrieval",
            "provider orchestration",
        ),
        "excludes": (
            "generic shell",
            "filesystem api",
            "generic core",
            "frontend",
            "adapter policy",
        ),
    },
    "web_frontend_implementer": {
        "owns": (
            "production ui",
            "approved ux",
            "api contracts",
        ),
        "excludes": (
            "backend",
            "auth",
            "canon",
            "adapter interpretation",
            "without inventing fields",
        ),
    },
}

HANDOFF_REQUIREMENTS = (
    "status",
    "outcome",
    "changed files",
    "design artifacts",
    "api",
    "schema",
    "migration",
    "verification commands",
    "actual outcomes",
    "deviations",
    "risks",
    "open decisions",
    "next action",
    "next owner",
)

AMBIGUITY_DOMAINS = (
    "user behavior",
    "scope",
    "ownership",
    "interfaces",
    "data",
    "security",
    "cost",
    "rollout",
)


def normalized(value):
    value = re.sub(r"[_-]+", " ", value.lower())
    return re.sub(r"\s+", " ", value).strip()


def load_agent(agent_name):
    path = AGENT_DIRECTORY / f"{agent_name}.toml"
    with path.open("rb") as handle:
        config = tomllib.load(handle)
    return config


class HostedAgentGovernanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.configs = {
            agent_name: load_agent(agent_name)
            for agent_name in AGENTS
            if (AGENT_DIRECTORY / f"{agent_name}.toml").is_file()
        }

    def test_planned_agent_configs_exist_and_parse(self):
        for agent_name in AGENTS:
            path = AGENT_DIRECTORY / f"{agent_name}.toml"
            with self.subTest(agent=agent_name):
                self.assertIn(
                    agent_name,
                    self.configs,
                    f"missing planned agent config: {path}",
                )
                config = self.configs[agent_name]
                self.assertEqual(agent_name, config.get("name"), path)
                self.assertTrue(config.get("description", "").strip(), path)
                self.assertTrue(config.get("developer_instructions", "").strip(), path)

    def test_routing_descriptions_distinguish_the_three_roles(self):
        descriptions = {
            agent_name: normalized(config["description"])
            for agent_name, config in self.configs.items()
        }

        expected_distinctions = {
            "product_designer": ("product strategist", "architect"),
            "hosted_backend_implementer": ("core implementer", "architect"),
            "web_frontend_implementer": ("core implementer", "product designer"),
        }
        for agent_name, distinctions in expected_distinctions.items():
            if agent_name not in descriptions:
                continue
            for distinction in distinctions:
                with self.subTest(agent=agent_name, distinction=distinction):
                    self.assertIn(distinction, descriptions[agent_name])

    def test_role_ownership_and_exclusions_are_explicit(self):
        for agent_name, expected in AGENTS.items():
            if agent_name not in self.configs:
                continue
            contract = normalized(
                self.configs[agent_name]["description"]
                + " "
                + self.configs[agent_name]["developer_instructions"]
            )
            for phrase in expected["owns"]:
                with self.subTest(agent=agent_name, owns=phrase):
                    self.assertIn(phrase, contract)
            for phrase in expected["excludes"]:
                with self.subTest(agent=agent_name, excludes=phrase):
                    self.assertIn(phrase, contract)

    def test_agents_md_owns_the_shared_delegated_work_protocol(self):
        instructions = normalized(AGENTS_PATH.read_text(encoding="utf-8"))
        self.assertRegex(
            instructions,
            r"first non empty line.*exactly one of.*ready.*decision required.*blocked",
        )
        self.assertIn("no material uncertainty or blocker exists", instructions)
        self.assertRegex(
            instructions,
            r"decision required.*choice could materially change",
        )
        self.assertRegex(instructions, r"(do not|never).*(assum|guess|invent)")
        for phrase in HANDOFF_REQUIREMENTS:
            with self.subTest(handoff_requirement=phrase):
                self.assertIn(phrase, instructions)
        for domain in AMBIGUITY_DOMAINS:
            with self.subTest(ambiguity_domain=domain):
                self.assertIn(domain, instructions)
        self.assertIn("reversible local detail", instructions)
        self.assertIn("explicit work package default", instructions)
        self.assertRegex(instructions, r"(disclos\w*|report\w*).*handoff")

    def test_new_roles_reference_shared_protocol_and_add_role_handoff(self):
        role_handoff_terms = {
            "product_designer": ("design artifacts", "ui states", "accessibility"),
            "hosted_backend_implementer": ("api", "schema", "migration"),
            "web_frontend_implementer": ("api contracts", "responsive", "accessibility"),
        }
        for agent_name, config in self.configs.items():
            instructions = normalized(config["developer_instructions"])
            with self.subTest(agent=agent_name):
                self.assertRegex(
                    instructions,
                    r"follow.*agents\.md.*delegated work protocol",
                )
                self.assertIn("handoff", instructions)
                for phrase in role_handoff_terms[agent_name]:
                    self.assertIn(phrase, instructions)


if __name__ == "__main__":
    unittest.main()
