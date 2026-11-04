-- Tabelas do lead-sync-service no Supabase (prefixo omnichat_).
--
-- Ja existem (criadas em 10/2026, usadas por db_supabase.py):
--   omnichat_poll_state      (id integer pk = 1, last_checked_at text)
--   omnichat_processed_chats (chat_id text pk, phone, name, action, rd_crm_deal_id, processed_at timestamptz)
--   omnichat_poll_runs       (id bigint pk, started_at/finished_at timestamptz, contadores, error)
--
-- Novas, da cadencia de WhatsApp - rode uma vez no SQL Editor:

create table if not exists omnichat_cadence_sends (
    deal_id text not null,
    stage_id text not null,
    deal_name text,
    phone text,
    action text not null,
    detail text,
    created_at timestamptz not null,
    primary key (deal_id, stage_id)
);

create table if not exists omnichat_cadence_stages_seeded (
    stage_id text primary key,
    seeded_at timestamptz not null
);
