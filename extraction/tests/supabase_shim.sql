-- Just enough of Supabase for the tests to run on a plain Postgres (the GitHub Actions job).
-- conftest.py runs this only when the `auth` schema does not exist, so it never touches a real Supabase
-- project, where these objects already exist and `auth.uid()` must not be replaced.
do $$
begin
  if not exists (select from pg_roles where rolname = 'anon') then create role anon nologin; end if;
  if not exists (select from pg_roles where rolname = 'authenticated') then create role authenticated nologin; end if;
  if not exists (select from pg_roles where rolname = 'service_role') then create role service_role nologin bypassrls; end if;
end
$$;

create schema if not exists auth;

create table if not exists auth.users (
  id    uuid primary key default gen_random_uuid(),
  email text
);

-- Supabase sets request.jwt.claim.sub from the caller's token; tests set it with `set local`.
create or replace function auth.uid() returns uuid
language sql stable
as $$ select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid $$;
