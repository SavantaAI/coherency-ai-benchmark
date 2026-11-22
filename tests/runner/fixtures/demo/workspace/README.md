# Batch jobs

Run `./bin/jobs submit data/<batch>.csv` for each batch. Each run prints a job id
and writes its log to `out/<job id>.log`. When every batch has run, write a
short summary of the jobs to `out/summary.txt`.
