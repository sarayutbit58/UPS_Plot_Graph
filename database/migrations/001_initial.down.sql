-- Drop dependents before parents; migration bookkeeping is runner-owned.
DROP TABLE IF EXISTS price_offers;
DROP TABLE IF EXISTS product_specifications;
DROP TABLE IF EXISTS product_sources;
DROP TABLE IF EXISTS import_rejections;
DROP TABLE IF EXISTS products;
DROP TABLE IF EXISTS source_files;
DROP TABLE IF EXISTS manufacturers;
DROP TABLE IF EXISTS topology_types;
DROP TABLE IF EXISTS form_factors;
DROP TABLE IF EXISTS currencies;
