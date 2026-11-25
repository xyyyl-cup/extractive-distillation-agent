# Running and reproducing the workflow

Read the source modules and their --help before executing calculation commands. The dashboard and tools may call paid model APIs or launch long Aspen jobs; configure them deliberately.

The source is copied without a broad rewrite. Some modules and evidence records contain original absolute Windows paths. Use the bundled docs/verified_paths.md and inspect configuration before executing. Historical source paths in records are provenance, not portable execution instructions.

Suggested sequence: launch the local dashboard; inspect a screening task; run the controller's optimization dry-run; configure Aspen templates and verify COM paths; evaluate a representative case in a separate working directory; inspect unit status, product constraints and component balances before optimization. Gaussian/NCI and GROMACS analysis require their own environments and input preparation.

The package has been assembled and scanned statically. Scientific calculations, commercial-software integration, API requests and Python dependency compatibility have not been rerun. Selected input/model and large raw-output dependencies remain in the original review archive. This is not yet a one-command reproduction of every manuscript figure.
