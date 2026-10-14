/**
 * The `default` of a switch over a union: fails to compile when a member is
 * unhandled (the value is not `never`), and throws on a value the types did
 * not allow (a server newer than this client).
 */
export function assertNever(value: never, what: string): never {
  throw new Error(`no catalog entry for ${what} "${String(value)}"`);
}
