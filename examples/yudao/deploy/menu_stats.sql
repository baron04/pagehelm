-- Pages (type 2) and buttons (type 3) per top-level menu, counting only
-- enabled, undeleted rows. Run against the yudao database.
WITH RECURSIVE tree AS (
  SELECT id, id AS root_id, name AS root_name FROM system_menu
   WHERE parent_id = 0 AND deleted = 0 AND status = 0
  UNION ALL
  SELECT m.id, t.root_id, t.root_name FROM system_menu m
   JOIN tree t ON m.parent_id = t.id
   WHERE m.deleted = 0 AND m.status = 0
)
SELECT t.root_name AS top_menu,
       SUM(m.type = 2) AS pages,
       SUM(m.type = 3) AS buttons
  FROM tree t JOIN system_menu m ON m.id = t.id
 GROUP BY t.root_id, t.root_name
 ORDER BY pages DESC;
