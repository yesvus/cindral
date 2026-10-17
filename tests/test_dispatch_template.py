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

        for input_name in ("cindral_lane", "cindral_target", "cindral_reason", "cindral_ref"):
            self.assertRegex(template, rf"(?m)^      {input_name}:")

        self.assertNotRegex(template, r"(?m)^      target:")
        self.assertEqual(template.count("ref: ${{ inputs.cindral_ref || github.ref }}"), len(policy["lanes"]) + 1)
        # The device job selects by runner label, not by naming devices in its
        # condition. A device runs the job only if it advertises the target the
        # broker chose, which the runner enforces and a boolean cannot; naming them
        # here instead bound the shipped template to three device names no policy
        # configures, so onboarding failed closed against every real deployment.
        self.assertIn(
            'runs-on: [self-hosted, Linux, ARM64, "${{ inputs.cindral_target }}"]', template
        )
        for target in policy["local"]["device_priority"]:
            self.assertNotIn(
                f"inputs.cindral_target == '{target}'", template,
                "the template names a device target in a condition rather than "
                "leaving the selection to the runner label",
            )

        for lane, config in policy["lanes"].items():
            match = re.search(
                rf"(?ms)^  {lane}:\n(.*?)(?=^  [a-z_]+:\n|\Z)",
                template,
            )
            self.assertIsNotNone(match, lane)
            labels = list(config["labels"])
            if lane == "device":
                labels.append('"${{ inputs.cindral_target }}"')
            expected = f"runs-on: [{', '.join(labels)}]"
            self.assertIn(expected, match.group(1), lane)

    def test_every_lane_keeps_the_output_a_run_wrote(self) -> None:
        # the device pool archives .cindral/artifacts, and a lane that runs on
        # a GitHub runner has to keep the same directory or a repository that
        # writes it gets its output back on one path and not the other
        template = (ROOT / "templates/personal-dispatch.yml").read_text()
        lanes = template.count("      - run: ./scripts/ci")
        self.assertEqual(template.count("path: .cindral/artifacts"), lanes)
        self.assertEqual(template.count("uses: actions/upload-artifact@"), lanes)
        # a run that wrote nothing must not fail on a missing upload
        self.assertEqual(template.count("if-no-files-found: ignore"), lanes)


if __name__ == "__main__":
    unittest.main()
