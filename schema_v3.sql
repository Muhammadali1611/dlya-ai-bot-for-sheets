-- 3-bosqich: odamlarga sizning profilingizdan yuborish.

-- Odam qaysi yozuvda xabar oladi: 'kiril' (standart) yoki 'lotin'
alter table eslatma.odamlar   add column if not exists alifbo text not null default 'kiril'
    check (alifbo in ('kiril', 'lotin'));
-- Telegram'dagi to'liq ismi (kontaktdagi) — tasdiqlash uchun ko'rsatiladi
alter table eslatma.odamlar   add column if not exists tg_ism text;

-- Vazifada aytilgan ism (masalan "Abbosxon aka") — odam hali tanlanmagan bo'lsa ham saqlanadi
alter table eslatma.vazifalar add column if not exists odam_ism text;

alter table eslatma.yuborishlar add column if not exists tg_chat_id bigint;
create index if not exists yuborishlar_ochiq_idx on eslatma.yuborishlar (holat, yuborilgan_at);

-- Bot sozlamalari (masalan, shifrlangan Telegram sessiyasi)
create table if not exists eslatma.sozlamalar (
    kalit      text primary key,
    qiymat     text,
    updated_at timestamptz not null default now()
);
alter table eslatma.sozlamalar enable row level security;

grant select, insert, update, delete on eslatma.sozlamalar to eslatma_bot;
drop policy if exists bot_hammasi on eslatma.sozlamalar;
create policy bot_hammasi on eslatma.sozlamalar for all to eslatma_bot using (true) with check (true);

-- Vazifa o'chirilsa/bajarilsa ham yuborishlar tarixi saqlansin
alter table eslatma.yuborishlar drop constraint if exists yuborishlar_vazifa_id_fkey;
alter table eslatma.yuborishlar add constraint yuborishlar_vazifa_id_fkey
    foreign key (vazifa_id) references eslatma.vazifalar(id) on delete set null;
alter table eslatma.yuborishlar add column if not exists vazifa_title text;
