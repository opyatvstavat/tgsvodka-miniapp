"""Copy the Postgres database into a SQLite file.

The Postgres container cost more in idle memory than the whole API process,
while holding ~100 MB of data. This moves that data onto the volume the web
service already mounts.

    SOURCE_DATABASE_URL=postgresql://…  python scripts/migrate_to_sqlite.py out.db

Reads are batched by primary key so a large table does not have to fit in
memory. Existing rows in the target are left alone, so a rerun resumes.
"""
import asyncio
import os
import sys

from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.models import Base, ChannelSyncState, Post  # noqa: E402

BATCH = 2000


def _normalize(url: str) -> str:
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+asyncpg://", 1)
    if url.startswith("postgresql://") and "+asyncpg" not in url:
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


async def copy_table(src_sessionmaker, dst_sessionmaker, model, order_col) -> int:
    copied = 0
    last = None

    while True:
        async with src_sessionmaker() as src:
            query = select(model).order_by(order_col).limit(BATCH)
            if last is not None:
                query = query.where(order_col > last)
            rows = list((await src.execute(query)).scalars().all())

        if not rows:
            break

        payload = [
            {c.name: getattr(row, c.name) for c in model.__table__.columns}
            for row in rows
        ]
        async with dst_sessionmaker() as dst:
            await dst.execute(
                insert(model).prefix_with("OR IGNORE"),
                payload,
            )
            await dst.commit()

        last = getattr(rows[-1], order_col.name)
        copied += len(rows)
        print(f"  {model.__tablename__}: {copied} rows", flush=True)

    return copied


async def main() -> None:
    source = os.getenv("SOURCE_DATABASE_URL")
    if not source:
        raise SystemExit("SOURCE_DATABASE_URL is required")
    target_path = sys.argv[1] if len(sys.argv) > 1 else "tgsvodka.db"

    src_engine = create_async_engine(_normalize(source))
    dst_engine = create_async_engine(f"sqlite+aiosqlite:///{target_path}")
    src_sessionmaker = async_sessionmaker(src_engine, expire_on_commit=False)
    dst_sessionmaker = async_sessionmaker(dst_engine, expire_on_commit=False)

    async with dst_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    print(f"Copying into {target_path}")
    # Keyset-paginate on the primary key: summary_msg_id is only unique per chat,
    # so paging on it would skip rows that share an id across channels.
    await copy_table(src_sessionmaker, dst_sessionmaker, Post, Post.id)
    await copy_table(src_sessionmaker, dst_sessionmaker, ChannelSyncState, ChannelSyncState.id)

    # Ingest keeps writing to the source while we copy, so a plain count
    # comparison races. Reconcile on primary keys instead and copy whatever
    # arrived mid-run, repeating until nothing is missing.
    for attempt in range(1, 6):
        async with src_sessionmaker() as src, dst_sessionmaker() as dst:
            src_ids = set((await src.execute(select(Post.id))).scalars().all())
            dst_ids = set((await dst.execute(select(Post.id))).scalars().all())
        missing = src_ids - dst_ids

        print(f"reconcile pass {attempt}: source={len(src_ids)} target={len(dst_ids)} "
              f"missing={len(missing)}")
        if not missing:
            break

        async with src_sessionmaker() as src:
            rows = list(
                (await src.execute(select(Post).where(Post.id.in_(list(missing))))).scalars().all()
            )
        payload = [{c.name: getattr(r, c.name) for c in Post.__table__.columns} for r in rows]
        async with dst_sessionmaker() as dst:
            await dst.execute(insert(Post).prefix_with("OR IGNORE"), payload)
            await dst.commit()
    else:
        raise SystemExit("source kept changing faster than reconciliation could keep up")

    async with src_sessionmaker() as src, dst_sessionmaker() as dst:
        for model in (Post, ChannelSyncState):
            a = (await src.execute(select(func.count()).select_from(model))).scalar_one()
            b = (await dst.execute(select(func.count()).select_from(model))).scalar_one()
            print(f"{model.__tablename__}: source={a} target={b}")
            if b < a:
                # Not fatal on its own: backfill re-adds anything newer from the
                # summary channel after cutover. Still worth seeing.
                print(f"  note: {a - b} row(s) arrived after the last reconcile pass")

        by_status = (
            await dst.execute(select(Post.status, func.count()).group_by(Post.status))
        ).all()
        print("target status breakdown:", dict(by_status))

    await src_engine.dispose()
    await dst_engine.dispose()
    print("done")


if __name__ == "__main__":
    asyncio.run(main())
