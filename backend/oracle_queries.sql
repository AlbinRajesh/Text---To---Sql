-- Read-only oracle queries E01 to E25. SELECT only.
-- Each query is preceded by a label row so you can tell the results apart.

SELECT '=== E01 ===' AS query_id;
SELECT sd.name, sa.city FROM shop_details sd JOIN subcategories sc ON sc.id = sd.subcategory_id JOIN shop_address sa ON sa.shop_id = sd.id AND sa.is_default = 1 AND sa.city = 'Nagercoil' WHERE sd.status = 1 AND sc.subcategoryname = 'Drinks & Beverages' ORDER BY sd.name;

SELECT '=== E02 ===' AS query_id;
SELECT sd.name, sa.city FROM shop_details sd JOIN shop_address sa ON sa.shop_id = sd.id AND sa.is_default = 1 AND sa.city = 'Nagercoil' WHERE sd.status = 1 AND sd.name LIKE '%juice%' ORDER BY sd.name;

SELECT '=== E03 ===' AS query_id;
SELECT sd.name, sa.city FROM shop_details sd JOIN shop_address sa ON sa.shop_id = sd.id AND sa.is_default = 1 AND sa.city = 'Nagercoil' WHERE sd.status = 1 AND sd.name LIKE '%bakery%' ORDER BY sd.name;

SELECT '=== E04 ===' AS query_id;
SELECT COUNT(*) AS active_shop_count FROM shop_details WHERE status = 1;

SELECT '=== E05 ===' AS query_id;
SELECT DISTINCT s.name AS shop_name FROM shop_details s JOIN subcategories sc ON sc.id = s.subcategory_id WHERE s.status = 1 AND sc.subcategoryname IN ('Snacks & Beverages', 'Drinks & Beverages');

SELECT '=== E06 ===' AS query_id;
SELECT time_slot_month AS booking_month, COUNT(*) AS booking_count FROM bookings WHERE status = 1 AND time_slot_date >= '2025-01-01' AND time_slot_date < '2026-01-01' GROUP BY time_slot_month HAVING COUNT(*) = (SELECT MAX(monthly.booking_count) FROM (SELECT COUNT(*) AS booking_count FROM bookings WHERE status = 1 AND time_slot_date >= '2025-01-01' AND time_slot_date < '2026-01-01' GROUP BY time_slot_month) AS monthly) ORDER BY booking_month;

-- E07 is corrected: the question asks for ONE total, not top 5 shops.
-- Check value: your earlier case_06 oracle returned 82191.0. If this differs, tell the AI.
SELECT '=== E07 ===' AS query_id;
SELECT SUM(final_amount) AS total_revenue FROM bookings WHERE status = 1 AND time_slot_date >= '2025-01-01' AND time_slot_date < '2026-01-01';

SELECT '=== E08 ===' AS query_id;
SELECT s.name AS shop_name, COUNT(*) AS review_count, ROUND(AVG(r.rating), 2) AS average_rating FROM reviews r JOIN shop_details s ON s.id = r.shop_id WHERE s.status = 1 GROUP BY s.id, s.name HAVING COUNT(*) > 2 ORDER BY review_count DESC, s.name;

SELECT '=== E09 ===' AS query_id;
SELECT id, name FROM shop_details WHERE status = 1 LIMIT 5;

SELECT '=== E10 ===' AS query_id;
SELECT sd.name, sa.city FROM shop_details sd JOIN subcategories sc ON sc.id = sd.subcategory_id JOIN shop_address sa ON sa.shop_id = sd.id AND sa.is_default = 1 WHERE sd.status = 1 AND sc.subcategoryname = 'Restaurent' ORDER BY sd.name;

SELECT '=== E11 ===' AS query_id;
SELECT sc.id, sc.subcategoryname FROM subcategories sc WHERE sc.subcategoryname LIKE '%Beverages%' ORDER BY sc.subcategoryname LIMIT 300;

SELECT '=== E12 ===' AS query_id;
SELECT sd.id, sd.name FROM shop_details sd WHERE sd.status = 1 AND EXISTS (SELECT 1 FROM shop_address sa WHERE sa.shop_id = sd.id AND sa.is_default = 1 AND sa.city = 'Nagercoil') AND EXISTS (SELECT 1 FROM subcategories sc WHERE sc.id = sd.subcategory_id AND sc.subcategoryname = 'Fitness') ORDER BY sd.name LIMIT 300;

SELECT '=== E13 ===' AS query_id;
SELECT sd.id, sd.name FROM shop_details sd WHERE sd.status = 1 AND sd.subcategory_id = (SELECT sc.id FROM subcategories sc WHERE sc.subcategoryname = 'Drinks & Beverages' LIMIT 1) AND EXISTS (SELECT 1 FROM shop_address sa WHERE sa.shop_id = sd.id AND sa.is_default = 1 AND sa.city = 'Nagercoil') ORDER BY sd.name LIMIT 1;

SELECT '=== E14 ===' AS query_id;
SELECT COUNT(DISTINCT sd.id) AS active_shop_count FROM shop_details sd JOIN shop_address sa ON sa.shop_id = sd.id WHERE sd.status = 1 AND sa.is_default = 1 AND sa.city = 'Nagercoil';

-- E15 needs MySQL 8.0 or newer (window function).
SELECT '=== E15 ===' AS query_id;
SELECT ranked.id, ranked.name, ranked.created_at FROM (SELECT sd.id, sd.name, sd.created_at, ROW_NUMBER() OVER (ORDER BY sd.created_at DESC, sd.id DESC) AS row_rank FROM shop_details sd WHERE sd.status = 1) AS ranked WHERE ranked.row_rank <= 5 ORDER BY ranked.row_rank;

SELECT '=== E16 ===' AS query_id;
SELECT sd.id, sd.name FROM shop_details sd JOIN shop_address sa ON sa.shop_id = sd.id JOIN subcategories sc ON sc.id = sd.subcategory_id WHERE sd.status = 1 AND sa.is_default = 1 AND sa.city = 'Thiruvananthapuram' AND sc.subcategoryname = 'Restaurent' GROUP BY sd.id, sd.name HAVING COUNT(*) > 0 ORDER BY sd.name LIMIT 300;

SELECT '=== E17 ===' AS query_id;
SELECT b.id, b.shop_id, b.created_at, b.final_amount FROM bookings b WHERE b.created_at >= '2025-01-01' AND b.created_at < '2026-01-01' ORDER BY b.created_at LIMIT 300;

SELECT '=== E18 ===' AS query_id;
SELECT sa.city, COUNT(DISTINCT sd.id) AS active_shop_count FROM shop_details sd JOIN shop_address sa ON sa.shop_id = sd.id AND sa.is_default = 1 WHERE sd.status = 1 GROUP BY sa.city ORDER BY active_shop_count DESC, sa.city LIMIT 300;

SELECT '=== E19 ===' AS query_id;
SELECT id, name FROM shop_details WHERE status = 1 AND id = 1 AND id = 2;

-- E20 is a vague question; the correct app behavior is a clarification, so zero rows is expected.
-- FROM DUAL added so the query is valid on every MySQL version.
SELECT '=== E20 ===' AS query_id;
SELECT 'clarification_required' AS result FROM DUAL WHERE 1 = 0;

SELECT '=== E21 ===' AS query_id;
SELECT sd.id, sd.name, (SELECT ROUND(AVG(r.rating), 2) FROM reviews r WHERE r.shop_id = sd.id) AS average_rating FROM shop_details sd JOIN shop_address sa ON sa.shop_id = sd.id AND sa.is_default = 1 WHERE sd.status = 1 AND sa.city = 'Nagercoil' AND (SELECT AVG(r.rating) FROM reviews r WHERE r.shop_id = sd.id) >= 4.0 ORDER BY average_rating DESC, sd.name LIMIT 300;

SELECT '=== E22 ===' AS query_id;
SELECT id, shop_id, user_id, rating, created_at FROM reviews LIMIT 300;

SELECT '=== E23 ===' AS query_id;
SELECT c.categoriesname AS category_name, sc.subcategoryname AS subcategory_name, sd.name AS shop_name FROM shop_details sd JOIN categories c ON c.id = sd.category_id JOIN subcategories sc ON sc.id = sd.subcategory_id WHERE sd.status = 1 ORDER BY c.categoriesname, sc.subcategoryname, sd.name LIMIT 300;

SELECT '=== E24 ===' AS query_id;
SELECT sd.id, sd.name FROM shop_details sd WHERE sd.status = 1 AND sd.phone IS NULL ORDER BY sd.name LIMIT 300;

SELECT '=== E25 ===' AS query_id;
SELECT sd.id, sd.name FROM shop_details sd WHERE sd.status = 1 AND NOT EXISTS (SELECT 1 FROM reviews r WHERE r.shop_id = sd.id) ORDER BY sd.name LIMIT 300;