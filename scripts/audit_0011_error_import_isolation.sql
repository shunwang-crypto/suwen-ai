-- Read-only audit for environments upgraded through 0011.
-- 0011 intentionally removed synthetic imported Attempts.  This report checks
-- whether any real-looking attempt or mastery evidence was attached to an
-- imported question; it never updates or deletes data.

SELECT
    'imported_attempts_remaining' AS check_name,
    count(*) AS row_count
FROM attempts a
JOIN error_items e ON e.question_id = a.question_id
WHERE e.source_import_id IS NOT NULL;

SELECT
    'imported_mastery_logs' AS check_name,
    count(*) AS row_count
FROM mastery_log ml
JOIN error_items e ON e.id = ml.evidence_id
WHERE e.source_import_id IS NOT NULL;

SELECT
    'imported_questions_with_quiz' AS check_name,
    count(*) AS row_count
FROM questions q
JOIN error_items e ON e.question_id = q.id
WHERE e.source_import_id IS NOT NULL
  AND q.quiz_id IS NOT NULL;

SELECT
    e.id AS error_item_id,
    e.source_import_id,
    a.id AS attempt_id,
    a.at AS attempt_at,
    ml.id AS mastery_log_id,
    ml.evidence_type,
    ml.at AS mastery_at
FROM error_items e
LEFT JOIN attempts a ON a.question_id = e.question_id
LEFT JOIN mastery_log ml ON ml.evidence_id = e.id
WHERE e.source_import_id IS NOT NULL
  AND (a.id IS NOT NULL OR ml.id IS NOT NULL)
ORDER BY e.id, a.at, ml.at;
