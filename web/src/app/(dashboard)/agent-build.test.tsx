import { describe, expect, it } from "vitest";

import type { DeviceInfo } from "@/lib/cindral";
import { agentBuild } from "./tables";

const device = (over: Partial<DeviceInfo> = {}): DeviceInfo =>
  ({ name: "device-alpha", status: "online", healthy: true, ...over }) as DeviceInfo;

describe("agentBuild", () => {
  it("shows the short commit a device reported", () => {
    const shown = agentBuild(
      device({ build_known: true, build: { commit: "0123456789abcdef", version: "0.13.0" } }),
    );
    expect(shown.label).toBe("01234567");
    expect(shown.title).toContain("0.13.0");
    expect(shown.title).toContain("0123456789abcdef");
  });

  it("says unknown for a device that reported no build", () => {
    // an older agent sends nothing. Reading that as current is the failure the
    // column exists to prevent
    expect(agentBuild(device({ build_known: false, build: {} })).label).toBe("unknown");
    expect(agentBuild(device({ build_known: true, build: { commit: "unknown" } })).label).toBe(
      "unknown",
    );
  });

  it("says unknown for a broker that sends no build field at all", () => {
    expect(agentBuild(device()).label).toBe("unknown");
  });
});
