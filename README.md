# Revelio Labs position coalescing

## 1. Overview

The input is a folder of Parquet files. Each file holds snapshots of employment profiles, and each snapshot shows a user's positions at one point in time. The same job appears again and again across snapshots, often with small differences.

The pipeline links those records into positions and writes one row per position per snapshot, `(position_id, asof_ts)`. Each position keeps the same `position_id` across all snapshots. A position that disappears from a later snapshot is carried forward, and conflicting values for the same position are reconciled.

The reasoning behind the design, the measurements and the limitations are in [TECHNICAL_DESIGN.md](TECHNICAL_DESIGN.md).

## 2. Requirements

- Docker. The image is a Linux `linux/amd64` image. On an Apple Silicon Mac, add `--platform linux/amd64` after `docker run`; it runs more slowly under emulation.
- 16 GB of memory available to Docker is recommended for the full dataset; that is the setup the run was measured on. On Docker Desktop (Mac, Windows), set this under Settings → Resources. Section 6 covers smaller machines.
- The input Parquet files, downloaded using the link in the assignment brief.

## 3. Run the pipeline

Run these commands from the repository root, the folder that contains this README and the supplied `position-coalescing.tar`.

**1. Load the image**

```bash
docker load -i position-coalescing.tar
```

**2. Put the input files in a folder called `input`** next to this README:

```
revelio-position-coalescing/
├── input/
│   ├── <file 1>.parquet
│   ├── <file 2>.parquet
│   └── ...
├── output/
├── position-coalescing.tar
└── README.md
```

Every `.parquet` file in `input/` is read, including files in subfolders.

**3. Run**

```bash
mkdir -p output
docker run --rm --cpus 4 --memory 16g -v "$PWD/input":/input:ro -v "$PWD/output":/output position-coalescing:latest
```

`--cpus 4 --memory 16g` reproduces the brief's environment. Both can be left out or changed. `output/` does not need to be empty: every run first deletes the `data_*.parquet` files left by an earlier run, then writes new ones. Other files in the folder are left alone.

## 4. Check the result

With 4 CPUs and 16 GB, the full dataset of 11.48 million rows takes about 70 seconds. When it finishes:

- The last log line is `all output checks passed`. The run exits with an error if any check fails.
- `output/` contains Parquet files named `data_*.parquet` with 493,509 positions and 13,541,452 rows.

These figures come from running the image in this package on the full dataset.

## 5. Run the tests

There are four kinds of checks, and they answer different questions:

| Check | What it shows | How to run it |
|---|---|---|
| Unit and pipeline tests (87) | Each rule behaves as designed, including examples that should and should not match | `python -m unittest discover -s tests -t .` (needs Python and `pip install -r requirements.txt`) |
| Brief example | Cases A and B from the brief come out as the brief shows | See below |
| Output checks | The output of a run is consistent | Run automatically at the end of every run (section 4) |
| Measurement scripts | The numbers quoted in TECHNICAL_DESIGN.md | Section 8 |

The tests should end with `OK`.

**Brief example, without the input data:** `examples/input/brief_cases.parquet` contains Example Cases A and B from the brief as two users (12345 = Case A, 12346 = Case B).

```bash
mkdir -p example_output
docker run --rm -v "$PWD/examples/input":/input:ro -v "$PWD/example_output":/output position-coalescing:latest
```

The output has 16 rows and 6 positions. On 2026-09-16, user 12345 still has both NYU positions (carried forward, Case A), and user 12346's Revelio Labs position shows `RL` / `MLE` with the same `position_id` and the description from 2026-09-01 (Case B).

## 6. Configuration

The pipeline reads the CPU and memory limits of the container when it starts and prints them on the first log line (`resources: ...`):

- DuckDB uses at most 60% of the memory (capped at 10 GB) and spills to disk beyond that.
- The matching stage uses one worker per CPU, but no more than one per 2 GB of memory.

The run time was measured with 4 CPUs and 16 GB; it was not measured with less memory. I ran a sample with 1 and 3 workers and different memory limits and got identical output, so the number of CPUs and the memory should only affect the run time.

If the run stops with exit code 137 (out of memory), lower the limits, for example:

```bash
docker run --rm -e MEMORY_LIMIT=3GB -e THREADS=2 -v "$PWD/input":/input:ro -v "$PWD/output":/output position-coalescing:latest
```

Optional environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `THREADS` | CPUs available to the container | Threads for DuckDB; also the maximum number of matching workers |
| `MEMORY_LIMIT` | 60% of available memory, at most `10GB` | Memory limit for DuckDB |
| `WORKER_MEMORY_LIMIT` | `1GB` | Memory limit for each matching worker |
| `WORK_DIR` | `/tmp/coalesce_work` | Folder for temporary files |
| `LARGE_USER_VERSIONS` | `1000` | Users with more versions than this are reported in the log |
| `ALLOW_COMPANY_RENAME`, `ALLOW_TITLE_EDIT`, `ALLOW_LEVEL_EDIT` | `1` | Set to `0` to turn off that matching rule |

## 7. Build from source

Use this instead of section 3, step 1, to build the image yourself:

```bash
docker build --platform linux/amd64 -t position-coalescing:latest .
docker save position-coalescing:latest -o position-coalescing.tar
```

To run without Docker (Python 3.11 or later):

```bash
pip install -r requirements.txt
python -m src.main --input input --output output
```

## 8. Reproduce the measurements

The measurements quoted in TECHNICAL_DESIGN.md come from four scripts in `checks/`. They are not part of the pipeline or the image. Run them from the repository root, with Python and the requirements installed and the input in `input/`. They produce reports from the input data; all except `cutoff_check.py` also run the pipeline to collect what they need. The reports contain real user IDs, so they are not committed.

| Script | What it measures |
|---|---|
| `checks/end_date_check.py` | Conflicting end dates within a snapshot, and how often each candidate rule agrees with the job's neighbouring single-copy snapshots |
| `checks/acronym_check.py` | Every merge made by the acronym rule, split by URL evidence |
| `checks/snapshot_check.py` | What the 1940 rule rejects and which of those jobs survive in kept snapshots |
| `checks/cutoff_check.py` | What each possible cut-off year would reject, and the date distribution before 1970 (reads the input only) |

```bash
python checks/end_date_check.py input > end_date_report.txt
THREADS=1 WORKER_MEMORY_LIMIT=6GB python checks/acronym_check.py input > acronym_report.txt
python checks/snapshot_check.py input > snapshot_report.txt
python checks/cutoff_check.py input > cutoff_report.txt
```

`acronym_check.py` needs `THREADS=1` because it watches the matcher from inside the same process. With a single worker, the default 1 GB worker memory limit is too small for the whole dataset and the script runs out of memory, so it also needs `WORKER_MEMORY_LIMIT=6GB`.

## 9. Output format

Columns: `asof_ts, user_id, company_raw, company_linkedin_url, title_raw, location_raw, description, startdate, enddate, position_id`.

- `asof_ts` is the snapshot. Each snapshot of a user lists every position known at that time, including positions carried forward from earlier snapshots.
- `position_id` is `"<user_id>-<n>"`, where `n` counts the user's positions in the order they were first seen. Re-running on the same input, or on the same input plus later snapshots, gives the same IDs.
- The other columns show the best-known values for the position at that snapshot, using only that snapshot and earlier ones.
- The input URL column may be named `company_linkedin_url` or `company_website`.

Every run checks its own output: no duplicate `(position_id, asof_ts)`, every output snapshot exists in the input, no end date before its start date, and no position disappearing from a later snapshot.

## 10. Code structure

| Stage | Tool | File |
|---|---|---|
| 1. Clean values, drop bad snapshots, collapse duplicates | DuckDB SQL | `src/prepare.py` |
| 2. Match records to positions, oldest snapshot first | Python worker processes, limited by the available CPUs and memory | `src/matching.py`, `src/match_stage.py` |
| 3. Build every snapshot, write Parquet, check the output | DuckDB SQL | `src/build_output.py` |

Settings are in `src/config.py`, and the entry point is `src/main.py`.

## 11. Further reading

[TECHNICAL_DESIGN.md](TECHNICAL_DESIGN.md) explains the data exploration, the cleaning and matching decisions, the challenges, the performance and scaling estimate, and the limitations.
