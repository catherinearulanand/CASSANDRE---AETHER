export default function Methodology() {
  return (
    <>
      <h1>Methodology and limitations</h1>
      <div className="card">
        <h2>Platform principles</h2>
        <ul className="muted">
          <li>Authoritative amounts are Decimal strings with explicit currency, sign and units; floating point is rejected at the API boundary.</li>
          <li>Deterministic services compute amounts and evaluate approved rules. Runtime models may propose extractions only; nothing is auto-activated.</li>
          <li>UNKNOWN is never PASS. Missing inputs block material actions with an explicit reason.</li>
          <li>Every consequential write appends a hash-chained evidence event in the same transaction. A hash chain alone cannot prove non-truncation; signed external checkpoints are required (see Evidence).</li>
          <li>No cross-tenant, cross-account or cross-master netting or collateral pooling unless an approved entitlement exists (default deny).</li>
        </ul>
        <p className="faint">Per-engine methodology pages are added by each engine owner as they ship; engines not yet built are marked “not built” in the navigation.</p>
      </div>
    </>
  );
}
