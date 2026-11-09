# Round 13 — reviewed order correction and measured K7 GO

2026-11-05. All four fresh Astra/xhigh reviews are complete and clean:

- [Admission, provenance and preservation](round-13-bindings.md)
- [Chronology, coverage and resources](round-13-coverage.md)
- [Regression and failure semantics](round-13-regressions.md)
- [Integration and disposition](round-13-integration.md)

**Supervising-phase disposition: measured K7 GO under accepted Stage 0**
for session `64f47629-48eb-4852-a1e6-e05d1c02207f`, this Nothing Phone (2a)
Android 16 build and the explicitly authorized Mac-USB variant. The phone
completed 8 h 0.655 s and 32/32 real turns with the accepted timing, resource,
thermal, identity, secure-screen-off, continuous-power, local-progress and
wake checks. Retirement, export, restoration and owned-helper cleanup are
verified. The separate Mac received all 32 exact reply records. See the
[actual report](../reports/s0b-phone-8h.md) and adjacent values. K1–K6 remain
closed by the [round-8 measurement review](round-8-results.md).

**R12-O1 (L1/S1/E1/I1), closed by reviewed additive evidence.** The original
failure is an evidence-processing false positive caused by treating
main-side callback completion order as worker-source chronology. Both
inversions remain visible; all 29,280 complete worker payloads match exactly
once against the strictly chronological worker journal. The separate
endpoint-selection defect is corrected: all 256 nearest chronological
coverage windows are reconstructed with their original main/worker partners.
Exactly four right endpoints change; counts, gaps and resource maxima do not.
All windows remain adequate, with no missing cycle of at least two seconds.
No source-clock, identity, lifecycle, coverage or resource violation remains
unexplained. All four reviewers independently inspected actual saved evidence
and found no important unresolved issue.

The [fresh correction phase](../reports/s0b-order-review-correction.md) added
only an offline review helper and meaningful regressions. Its 27 focused
cases plus 41 existing auditor cases passed, and lint passed. The one actual
additive invocation remains `ORDER_RECONCILED_REVIEW_PENDING`, SHA256
`15eba8f3bcf636db00c08814611f1e783d5369f653483c332a5fff86b0e9fd8c`.
The original audit remains `OBSERVED_FAILURE_REVIEW_REQUIRED`, sole failure
`paired_worker:out_of_order_sample_clock`, SHA256
`32c9edc549a0b0c329de322c7078c15f494cb6ed038a73c17b2eea474e56c1ac`.
Both retain `assigns_go=false`. Neither tool grants GO or rewrites evidence.
The decision above is the explicit supervising review of their combined,
independently bound evidence after all eight R12/R13 reviews. Its separate
dated receipt is private `s0b-order-review-01/k7-disposition-01.json`.
Pre-disposition report/values snapshots remain hash-bound in the private
documentation-baseline receipt; live status prose is updated separately.

R12-C1 remains recovered, with the original collector failure and one-line
parser recovery preserved. R12-I3 remains informational/closed with the
failed local pinned-source lookup honestly labelled. The isolated canary
output-limit error is retained; every phone request succeeded and no phone
failure coincides. No measurement, provider request or consumed fixture was
repeated. No accepted limit or Android/native artifact changed.

This completes the authorized Stage-0 K1–K7 measurement and review work. It
establishes limited screen-off viability on this setup. It does not establish
wall-charger operation, boot/restart recovery, chat-watcher delivery or 24/7
availability. **Stage 1 remains unaccepted and frozen.** No automatic rerun,
phone action, production implementation, staging or commit follows. No human
action is needed to finish this completed experiment.
