-- Companion to harden.py: disable the demo tenants and revoke every existing login.
UPDATE system_tenant SET status = 1 WHERE id <> 1;
DELETE FROM system_oauth2_access_token;
DELETE FROM system_oauth2_refresh_token;
-- The upstream's own links in the menu (its blog and documentation sites): not this site's.
UPDATE system_menu SET deleted = 1 WHERE path LIKE 'http%://%iocoder.cn%';
-- Top-level areas whose server side this deployment does not run: IoT needs TDengine, the
-- report module's closed JimuReport starter breaks /v3/api-docs, and CMS and EAM have no
-- open-source module. A disabled menu is not shown.
UPDATE system_menu SET status = 1 WHERE parent_id = 0 AND path IN ('/iot', '/report', '/cms', '/eam');
UPDATE system_menu SET status = 0 WHERE parent_id = 0 AND path = '/ai';
