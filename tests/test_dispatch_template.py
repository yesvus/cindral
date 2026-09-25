from pathlib import Path
import re
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[1]


class DispatchTemplateTest(unittest.TestCase):
    def test_dispatch_inputs_and_runner_labels_match_policy(self) -> None:
        template = (ROOT / "templates/personal-dispatch.yml").read_text()
        with (ROOT / "config/policy.toml").open("rb") as stream:
            policy = tomllib.load(stream)

        for input_name in ("relay_lane", "relay_target", "relay_reason"):
            self.assertRegex(template, rf"(?m)^      {input_name}:")

        self.assertNotRegex(template, r"(?m)^      target:")
        target_guard = " || ".join(
            f"inputs.relay_target == '{target}'"
            for target in policy["local"]["device_priority"]
        )
        self.assertIn(f"&& ({target_guard})", template)

        for lane, config in policy["lanes"].items():
            match = re.search(
                rf"(?ms)^  {lane}:\n(.*?)(?=^  [a-z_]+:\n|\Z)",
                template,
            )
            self.assertIsNotNone(match, lane)
            labels = list(config["labels"])
            if lane == "device":
                labels.append('"${{ inputs.relay_target }}"')
            expected = f"runs-on: [{', '.join(labels)}]"
            self.assertIn(expected, match.group(1), lane)


if __name__ == "__main__":
    unittest.main()
