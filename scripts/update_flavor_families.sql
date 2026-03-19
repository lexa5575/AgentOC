-- ============================================================
-- Update flavor_family for all product_catalog entries
-- New taxonomy (5 categories + device):
--   tobacco       — pure tobacco, no menthol, no capsule
--   menthol       — pure menthol/mint, no significant fruit
--   menthol_fruit — menthol + significant fruit/citrus notes
--   fruit         — fruity, no menthol
--   capsule       — Pearl/click products (flavor capsule technology)
--   device        — hardware (ONE, PRIME, STND) — unchanged
--
-- Key insight: same stock_name across regions = same flavor family.
-- Updates by stock_name apply to ALL regions simultaneously.
-- ============================================================

BEGIN;

-- ── TOBACCO ─────────────────────────────────────────────────
UPDATE product_catalog SET flavor_family = 'tobacco'
WHERE stock_name IN (
    'Amber',         -- classic tobacco (Armenia, KZ_HEETS, KZ_TEREA, EU)
    'Beige',         -- light balanced tobacco (Armenia)
    'Bronze',        -- medium-strong tobacco (Armenia, KZ, EU)
    'Clear',         -- clean light tobacco (Japan unique)
    'Golden',        -- classic golden tobacco (Indonesia)
    'KONA',          -- roasted tobacco with Kona coffee notes (EU)
    'Russet',        -- bold full-bodied tobacco (EU)
    'Siena',         -- smooth tobacco + light tea, Armenia spelling
    'Sienna',        -- same flavor, EU spelling
    'Silver',        -- light toasted tobacco + spicy herbs (Armenia, KZ, EU)
    'Sof Fuse',      -- mellow tobacco + subtle apple notes, NO capsule (EU)
    'T Balanced',    -- balanced tobacco (Japan)
    'T Regular',     -- regular tobacco (Japan)
    'T RICH',        -- rich tobacco (Japan)
    'T Smooth',      -- smooth tobacco (Japan)
    'Teak',          -- mellow tobacco + cream + nutty (Armenia, EU)
    'Warm Regular',  -- warm tobacco (Japan unique)
    'Yugen'          -- Indonesian tobacco + jasmine/lavender/pear aromas
);

-- ── MENTHOL ─────────────────────────────────────────────────
UPDATE product_catalog SET flavor_family = 'menthol'
WHERE stock_name IN (
    'BLUE',           -- strong icy menthol/peppermint (KZ, EU)
    'Bright Menthol', -- crisp menthol (Japan unique)
    'Green',          -- balanced fresh menthol (Armenia, EU)
    'T Black',        -- intense black menthol, NOT classic tobacco (Japan)
    'T Menthol',      -- menthol (Japan)
    'T Mint',         -- mint (Japan)
    'Turquoise'       -- gentle menthol (Armenia, KZ, EU)
);

-- ── MENTHOL_FRUIT ───────────────────────────────────────────
UPDATE product_catalog SET flavor_family = 'menthol_fruit'
WHERE stock_name IN (
    'AL Ruby WAVE',          -- menthol + red berries + floral (EU)
    'Black Purple Menthol',  -- dark berry + strong menthol (Japan + Japan unique)
    'Black Ruby Menthol',    -- red berry + cool menthol (Japan unique)
    'Black Tropical Menthol',-- mango + pineapple + menthol (Japan unique)
    'Black Yellow Menthol',  -- citrus/lemon + strong menthol (Japan unique)
    'Fusion Menthol',        -- blackberry + blossom + menthol (Japan unique)
    'Kelly',                 -- intense menthol + sharp citrus/lime (EU)
    'MAUVE',                 -- menthol + blueberry/blackberry (EU)
    'Ruby WAVE',             -- menthol + red garden berries + floral (EU)
    'Willow'                 -- crisp menthol + citrus + herbal notes (EU)
);

-- ── FRUIT ───────────────────────────────────────────────────
UPDATE product_catalog SET flavor_family = 'fruit'
WHERE stock_name IN (
    'Oasis',        -- citrus/tropical (Armenia, EU)
    'Oasis JP',     -- citrus/tropical Japan version (Japan unique)
    'Purple',       -- sweet berry / blueberry (Armenia, KZ, EU)
    'PURPLE',       -- same flavor, Indonesia
    'Ruby',         -- red berry / ruby fruit (Armenia, KZ_HEETS, KZ_TEREA)
    'Ruby FUSE',    -- berry + floral aromas, no menthol (EU)
    'Ruby Regular', -- pure berry, no menthol (Japan unique)
    'SAKURA',       -- cherry blossom + floral + subtle fruit (EU)
    'Summer',       -- summer citrus/tropical (Armenia, KZ, EU)
    'T Lemon',      -- bright lemon citrus (Japan)
    'T Purple',     -- berry / blueberry (Japan)
    'T Tropical',   -- tropical fruit (Japan)
    'Yellow',       -- citrus / zesty lemon (Armenia, KZ, EU)
    'Zing'          -- zesty citrus (KZ_TEREA)
);

-- ── CAPSULE ─────────────────────────────────────────────────
UPDATE product_catalog SET flavor_family = 'capsule'
WHERE stock_name IN (
    'Abore Pearl',  -- tobacco + green apple + menthol capsule (EU)
    'Amelia',       -- tobacco + watermelon + menthol capsule (EU, Latin)
    'АМЕЛИЯ',       -- same product, Cyrillic name (EU)
    'BRIZA',        -- roasted tobacco + woody/tea + menthol capsule (EU)
    'Oasis Pearl',  -- tobacco + tropical fruit + menthol capsule (EU)
    'Perint Pearl', -- capsule variant (Indonesia)
    'Starling',     -- tobacco + strawberry + basil + menthol capsule (Armenia)
    'Sun Pearl',    -- tobacco + exotic fruit + menthol capsule (Armenia, EU)
    'Twilight',     -- tobacco + blueberry + menthol capsule (EU)
    'Velvet Pearl'  -- berry + menthol capsule (Japan unique)
);

-- ── DEVICE ──────────────────────────────────────────────────
-- Already set to 'device' — no change needed.

COMMIT;

-- ── VERIFY ──────────────────────────────────────────────────
SELECT flavor_family, count(*) as products
FROM product_catalog
GROUP BY flavor_family
ORDER BY flavor_family;
