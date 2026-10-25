-- Add indexes for city/default-address filtering and subcategory-name lookup.
-- Apply once to each application database; inspect SHOW INDEX before rerunning.
CREATE INDEX idx_shop_address_city_default_shop
    ON shop_address (city, is_default, shop_id);

CREATE INDEX idx_subcategories_subcategoryname
    ON subcategories (subcategoryname);
