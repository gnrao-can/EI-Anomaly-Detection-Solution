/**
 * Minimal ambient declarations for jest-axe. The package ships without
 * bundled TypeScript types in this pinned version, and no @types/jest-axe
 * release matches it, so we declare only the surface we use.
 */
declare module 'jest-axe' {
  export interface AxeViolation {
    id: string;
    impact?: string | null;
    description: string;
    help: string;
    nodes: unknown[];
  }

  export interface AxeResults {
    violations: AxeViolation[];
    passes: unknown[];
    incomplete: unknown[];
    inapplicable: unknown[];
  }

  export function axe(
    html: Element | string,
    options?: Record<string, unknown>,
  ): Promise<AxeResults>;

  export function configureAxe(
    options?: Record<string, unknown>,
  ): typeof axe;

  export const toHaveNoViolations: {
    toHaveNoViolations(results: AxeResults): {
      pass: boolean;
      message(): string;
    };
  };
}
