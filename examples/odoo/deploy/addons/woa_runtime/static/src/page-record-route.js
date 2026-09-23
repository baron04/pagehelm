// Optional application-owned form-record identity. The generic Runtime cannot infer this
// from a URL or visible name; it treats this path only as an observation, never authority.
export function readPageRecordRoute() {
  return window.__woaRecordBridge?.current() ?? undefined;
}
