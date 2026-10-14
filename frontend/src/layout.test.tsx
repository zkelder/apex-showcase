import { readFileSync } from "node:fs";

import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

// See src/test/node-fs.d.ts for why "node:fs" resolves under this
// browser-targeted tsconfig. vite-node (vitest's module runner) shims
// __dirname at runtime even though this is an ESM module.
declare const __dirname: string;

// The real stylesheet's raw text, read directly off disk rather than
// `import "./styles.css"`, since vitest.config.ts sets `css: false` (which
// stubs *all* .css imports, `?raw` included, to an empty module). Reading
// the file this way still guarantees a byte-for-byte copy of what
// production ships -- never a hand-transcribed guess that could silently
// drift from styles.css.
const stylesheetText = readFileSync(`${__dirname}/styles.css`, "utf-8");

/** Mirrors the app shell markup every view in main.tsx renders (see e.g. the
 * default `App` return: `<div className="layout"><nav>...</nav>
 * <section className="content" id="main-content">...</section></div>`).
 * Kept hand-authored and data-free (no fetched research content) rather than
 * mounting a real page, so this stays a pure layout/CSS contract test that
 * cannot be perturbed by unrelated research/data-shape changes elsewhere in
 * the console -- and adds one genuinely wide table, matching the app's own
 * pattern (`.panel > .scroll > table`) of scoping wide tables to a local
 * horizontal scroller instead of letting them dictate page width. */
function ConsoleShellFixture() {
  const longCell = "x".repeat(220); // one unbreakable token; no spaces to wrap on
  return (
    <main>
      <header>
        <div className="brand">APEX <span>OPERATOR CONSOLE</span></div>
      </header>
      <div className="layout">
        <nav aria-label="Operator Console job navigation">
          <button className="active">Home</button>
          <button>Opportunities</button>
        </nav>
        <section className="content" id="main-content">
          <div className="panel">
            <div className="scroll">
              <table>
                <thead>
                  <tr>
                    <th scope="col">A</th>
                    <th scope="col">B</th>
                    <th scope="col">C</th>
                  </tr>
                </thead>
                <tbody>
                  <tr>
                    <td>{longCell}</td>
                    <td>{longCell}</td>
                    <td>{longCell}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </section>
      </div>
    </main>
  );
}

describe("operator console shell layout", () => {
  it("gives the .layout grid's content column a shrink boundary, so it can give up its max-width preference for the available track instead of forcing page-wide horizontal scroll", () => {
    const style = document.createElement("style");
    style.textContent = stylesheetText;
    document.head.appendChild(style);
    try {
      const { container } = render(<ConsoleShellFixture />);
      const layout = container.querySelector(".layout") as HTMLElement;
      const content = container.querySelector(".content") as HTMLElement;
      const scroll = container.querySelector(".scroll") as HTMLElement;

      expect(getComputedStyle(layout).display).toBe("grid");
      // The regression this guards: a grid item's automatic minimum width
      // defaults to its content's min-content size (here, the wide table),
      // which silently overrides `max-width` and pushes the whole page wider
      // than the viewport. min-width: 0 lets `.content` actually shrink to
      // its `1fr` track instead of dictating page width.
      expect(getComputedStyle(content).minWidth).toBe("0px");
      // The wide table's own local scroller must stay wired -- this fix
      // must not come at the cost of the app's existing "scroll wide tables
      // locally" pattern.
      expect(getComputedStyle(scroll).overflowX).toBe("auto");
    } finally {
      document.head.removeChild(style);
    }
  });
});
