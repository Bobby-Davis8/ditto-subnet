// Package probe drives the native enforcement evidence collectors over the
// production coding executor and sandbox launch code and assembles records in
// the catalog's v1 evidence schema, so the offline Python verifier
// (infra/scripts/coding-native-evidence.py) recomputes every matched value and
// accepts them.
//
// The runner never reimplements a container spec. Resource and cleanup evidence
// is measured on containers created by the exact production launch code:
//
//   - the harness class through sandbox.LocalDocker (internal/sandbox), the
//     same 65532:65532, read-only, --pids-limit, --ulimit and 8 MiB local-log
//     spec the validator runs;
//   - the executor_authoring and executor_grading classes through
//     codingexecutor.Executor's create+inspect+cleanup path, the same
//     10001:10001, --pull never, --memory-swap, cap-drop/cap-add and mount
//     policy the hosted grader runs.
//
// Pre-exec confinement runs public hostile fixtures on each approved runtime
// image through the production hosted-grading test path.
//
// This package mints no approval, reads no custody path and reaches no network
// beyond the local Docker daemon. It is default-off: nothing here runs from a
// host workflow, and the network kind (which needs nft/host-firewall authority)
// is deliberately deferred to a later PR behind the same Runner interface.
package probe
