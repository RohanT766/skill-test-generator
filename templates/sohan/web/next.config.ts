import type { NextConfig } from "next";

const port = process.env.PORT || process.env.APP_PORT || "3000";

const nextConfig: NextConfig = {
  distDir: process.env.NEXT_DIST_DIR || `/tmp/.next-${port}`,
  serverExternalPackages: ["@electric-sql/pglite"],
  typescript: { ignoreBuildErrors: true },
};

export default nextConfig;
