-- Eslatma bot v2 — Supabase (Postgres) bazasi.
-- Hammasi alohida "eslatma" bo'limida (schema): boshqa loyihalar jadvallariga tegilmaydi.

create schema if not exists eslatma;

-- Vazifa yuboriladigan odamlar (Abbosxon aka, Akmal aka, ...)
create table if not exists eslatma.odamlar (
    id          bigserial primary key,
    ism         text        not null,
    tg_user_id  bigint      unique,
    username    text,
    telefon     text,
    izoh        text,
    created_at  timestamptz not null default now()
);

-- Vazifalar
--   kind:    once (bir martalik) | recurring (kalendar bo'yicha) | interval (har N kunda)
--   status:  faol | bitdi (takroriy, shu navbat bajarildi) | pauza (eslatilmaydi)
--   next_at: keyingi eslatma vaqti; NULL = navbatda yo'q (bir martalik vaqti o'tgan yoki pauza)
--   bitguncha: true bo'lsa — "bitdi" deyilmaguncha takrorlanadi, bitdi deyilganda butunlay yopiladi
create table if not exists eslatma.vazifalar (
    id            bigserial primary key,
    title         text        not null,
    kind          text        not null check (kind in ('once', 'recurring', 'interval')),
    trigger       jsonb       not null,
    human_time    text        not null default '',
    next_at       timestamptz,
    status        text        not null default 'faol' check (status in ('faol', 'bitdi', 'pauza')),
    bitguncha     boolean     not null default false,
    odam_id       bigint      references eslatma.odamlar(id) on delete set null,
    xabar_matni   text,
    last_fired_at timestamptz,
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now()
);
create index if not exists vazifalar_next_at_idx on eslatma.vazifalar (next_at) where next_at is not null;

-- Odamlarga yuborilgan xabarlar va ularning kuzatuvi (3-bosqichda ishlatiladi)
create table if not exists eslatma.yuborishlar (
    id               bigserial primary key,
    vazifa_id        bigint      references eslatma.vazifalar(id) on delete cascade,
    odam_id          bigint      references eslatma.odamlar(id) on delete set null,
    matn             text        not null,
    tg_msg_id        bigint,
    yuborilgan_at    timestamptz not null default now(),
    oqilgan_at       timestamptz,
    sorash_soni      int         not null default 0,
    oxirgi_sorash_at timestamptz,
    javob            text,
    javob_turi       text,
    ai_qaror         jsonb,
    holat            text        not null default 'yuborildi'
);

-- Bajarilgan ishlar tarixi
create table if not exists eslatma.tarix (
    id         bigserial primary key,
    vazifa_id  bigint,
    title      text        not null,
    human_time text        not null default '',
    done_at    timestamptz not null default now(),
    izoh       text
);
create index if not exists tarix_done_at_idx on eslatma.tarix (done_at desc);

-- Xavfsizlik: public API (anon/authenticated) bu bo'limga umuman kira olmaydi.
-- Faqat bot o'zining alohida login'i (eslatma_bot) bilan ishlaydi.
alter table eslatma.odamlar     enable row level security;
alter table eslatma.vazifalar   enable row level security;
alter table eslatma.yuborishlar enable row level security;
alter table eslatma.tarix       enable row level security;
