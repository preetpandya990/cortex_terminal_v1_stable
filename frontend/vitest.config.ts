import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import tsconfigPaths from 'vite-tsconfig-paths';

/**
 * Vitest configuration for the Cortex AI frontend.
 *
 * Environment: jsdom — chosen over happy-dom for full CSS cascade support,
 * complete event propagation, and battle-tested stability.  The test suite
 * is expected to stay well under the threshold (~1000 tests) where happy-dom's
 * 2.5× speed advantage would meaningfully affect CI time.
 *
 * Path aliases: vite-tsconfig-paths reads tsconfig.json automatically so
 * the `@/*` → `./src/*` mapping requires no manual configuration here.
 */
export default defineConfig({
  plugins: [
    tsconfigPaths(),   // reads tsconfig.json paths: { "@/*": ["./src/*"] }
    react(),           // JSX transform + fast refresh (no-op in test env)
  ],
  test: {
    environment: 'jsdom',

    // Inject describe / it / expect / vi as globals, matching Jest convention
    // and keeping test files free of explicit imports for these primitives.
    globals: true,

    // Run the setup file before every test module.  Order matters:
    // 1. jest-dom matchers must be registered before the first assertion.
    // 2. next/navigation and other module-level mocks are hoisted here.
    setupFiles: ['./vitest.setup.ts'],

    // Collect tests from co-located __tests__ dirs.
    include: [
      'src/**/*.{test,spec}.{ts,tsx}',
    ],

    // Exclude generated Next.js output, Storybook stories, and the legacy
    // chart_policy test which uses Node's built-in `node:test` runner (not Vitest).
    // That file is retained as documentation of the chart-source routing policy.
    exclude: [
      '**/node_modules/**',
      '**/.next/**',
      '**/*.stories.{ts,tsx}',
      'tests/**',
    ],

    coverage: {
      provider: 'v8',
      reporter: ['text', 'json', 'html', 'lcov'],
      // Source files included in coverage reporting
      include: ['src/**/*.{ts,tsx}'],
      exclude: [
        'src/**/*.d.ts',
        'src/**/*.test.{ts,tsx}',
        'src/**/*.stories.{ts,tsx}',
        // shadcn/ui primitives — third-party wrappers, not our logic
        'src/components/ui/**',
        // Next.js App Router pages and layouts — integration-tested separately
        'src/app/**',
      ],
      // Minimum coverage thresholds — enforced on every CI run.
      // Focus is on the lib/ utilities that contain testable business logic.
      thresholds: {
        lines:      60,
        functions:  60,
        branches:   55,
        statements: 60,
      },
    },
  },
});
