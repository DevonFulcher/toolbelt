// Which build of the page this is, so a page left open across an update can say
// so. Vite names the main script after a hash of its content (`index-<hash>.js`);
// the server reports the same name for the build it serves (`served_build`).

/** The build this page runs, or ``null`` when it cannot be told (the dev server,
 * tests): such a page is never called out of date. */
export function runningBuild(doc: Document = document): string | null {
  const src = doc.querySelector('script[type="module"][src*="/assets/"]')?.getAttribute("src");
  return src ? (src.split("/").pop() ?? null) : null;
}
