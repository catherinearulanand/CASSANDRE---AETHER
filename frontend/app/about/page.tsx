export default function About() {
  return (
    <>
      <h1>About / Provenance</h1>
      <div className="card" style={{ display: "flex", gap: 24, alignItems: "center", flexWrap: "wrap" }}>
        <img src="/brand/cassandre-logo.jpg" alt="Cassandré logo" style={{ width: 160, borderRadius: 8 }} />
        <div style={{ flex: 1, minWidth: 260 }}>
          <p>
            AETHER originated as an OTC margin and collateral intelligence innovation and progressed through GIFT IFIH&apos;s Young Builders
            Programme. Following reported selection among the top 10 from 600+ teams, a two-month residential journey at GIFT City, industry
            and investor engagement and startup initiation, the team is extending its prototype into regulatory-first derivatives decision
            orchestration.
          </p>
          <p className="muted">
            These are founder-reported milestones, not institutional endorsements, paid customers, investment commitments, sandbox admission or
            regulatory validation. Supporting evidence is shown only if supplied and consented for publication.
          </p>
        </div>
      </div>
      <div className="card" style={{ marginTop: 16 }}>
        <h2>Earlier prototype figures (historical reference only)</h2>
        <p className="muted">The original prototype documented 406 passing tests, a USD 9.5m synthetic collateral call and 15 hash-linked events in one run. Its replay extraction was hand-authored, its IM methods were not SIMM, and it had no authentication, persistence or tenancy. These are not results of this build.</p>
      </div>
    </>
  );
}
