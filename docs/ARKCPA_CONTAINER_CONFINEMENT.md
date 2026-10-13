# Web container confinement

The `slowbooks` service runs with a read-only root filesystem, all Linux
capabilities dropped, and `no-new-privileges`. Only the existing uploads and
backups volumes remain persistently writable. A 256 MiB `/tmp` tmpfs supplies
temporary parser files with `noexec,nosuid,nodev`; its memory counts toward
the container's 2 GiB memory limit. The process/thread limit is 128.

PostgreSQL, controller-worker, environment, ports, existing volumes, application
code, and runtime dependencies are unchanged. These settings apply to the web
service only. Docker's existing default swap behavior is unchanged; `mem_limit`
is a memory cap, not an assertion that swap is disabled.

## Sizing evidence

On 2026-10-13, the reviewed live image
`sha256:924aeb975976a55d7ab4426eabf8d3d89af241a472291f91375dfcf390fb862c`
used two Uvicorn workers. Read-only cgroup measurements showed approximately
240 MiB current memory, 479 MiB historical peak, and 18 processes/threads.

An isolated copy of that exact image ran its default entrypoint against two
synthetic PostgreSQL databases, migrating both from `f9a0b1c2d3e4` to
`aa1b2c3d4e5f`. Eight concurrent HTTP clients completed eight invoice PDF
requests and eight OCR uploads, including 2550 by 3300 pixel receipt images
and multipage PDFs. Real WeasyPrint, poppler, Tesseract, and pg_dump executed
under the filesystem/capability restrictions. The harness also checked normal
authenticated reads, rejected invalid/anonymous uploads, and retained uploads
and backup output. Forty-five document/confinement checks passed.

The exploratory container peaked at 860,823,552 memory bytes (821 MiB),
36 processes/threads, and 282,624 sampled temporary-file bytes. Measurements
include the in-container test client and its eight threads. The chosen 2 GiB
memory cap is approximately 2.5 times that observed peak; 128 processes/threads
is over 3.5 times the observed count. A 256 MiB tmpfs leaves space for concurrent
uploads approaching the existing 20 MiB limit and ordinary 300-DPI page
rasters. Memory/PID limits were not hit during exploration.

The exploratory runner used higher safety ceilings only to measure demand.
Final acceptance must replay against a clean committed image under the actual
2 GiB / 128 / 256 MiB limits. It must include two 19 MiB padded valid PDFs,
ordinary pages through native browser login, real startup and both migrations,
real PDF/OCR/backup workflows, denied root writes, writable uploads/backups,
zero cgroup OOM/PID-limit events, and a healthy default healthcheck.

Private replay evidence and the final exact image/revision receipt live at:
`/home/novaadmin/ark-private-evidence/cpa-container-confinement-20261013/`.
The source-bound image build must leave Dockerfile and dependency locks unchanged.
Compare installed package versions and runtime package bytes with the reviewed
image before transferring its scanner/applicability assessment.

## Limits and deployment boundary

This is a bounded representative workload, not proof that every permitted
document or arbitrary concurrent demand fits. A compressed or unusually large
page can still exhaust a quota. The caps prevent unbounded host consumption;
they do not implement request admission, graceful cancellation, or parser
sandboxing. Changes to worker count, input sizes, or workload require another
measured replay before resizing.

Confinement does not patch any CVE. The vulnerability report for the reviewed
image remains open. An exploited in-process/native document parser still shares
the application's credentials and can access application-authorized databases
and writable uploads/backups. A separately isolated, least-privilege document
worker is a follow-up architectural mitigation, outside this change.

This source change and its synthetic tests do not authorize or perform live
deployment. An activation review must bind the exact qualified image and rendered
Compose policy, preserve live database/controller ownership, and record a rollback
to the prior reviewed web image and Compose policy.
