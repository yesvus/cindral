import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // the panel runs as a container, so a standalone server keeps the runtime
  // image to node plus the build output instead of the whole toolchain
  output: "standalone",
};

export default nextConfig;
