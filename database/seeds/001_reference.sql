-- Classification references only. These are not product or commercial claims.
INSERT OR IGNORE INTO topology_types(code, label) VALUES
    ('offline', 'Offline / standby'),
    ('line-interactive', 'Line interactive'),
    ('online-double-conversion', 'Online double conversion');
INSERT OR IGNORE INTO form_factors(code, label) VALUES
    ('tower', 'Tower'),
    ('rackmount', 'Rack mount'),
    ('rack-tower', 'Rack / tower convertible');
INSERT OR IGNORE INTO currencies(code, label) VALUES
    ('THB', 'Thai baht'),
    ('USD', 'US dollar'),
    ('EUR', 'Euro');
