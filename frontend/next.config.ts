import type { NextConfig } from "next";
import path from "node:path";

const projectRoot = path.resolve(__dirname);
// The monorepo root (one directory above frontend/).  Turbopack uses this to
// build its module-resolution search path, so setting it to the workspace root
// lets Turbopack find packages that npm hoisted to root node_modules rather
// than the workspace-level node_modules.
const monorepoRoot = path.resolve(__dirname, "..");

const nextConfig: NextConfig = {
  turbopack: {
    root: monorepoRoot,
    resolveAlias: {
      tailwindcss: path.join(projectRoot, "node_modules", "tailwindcss"),
    },
  },
};

export default nextConfig;
