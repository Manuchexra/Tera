"""SQL matnini PostgreSQL uchun moslash. Kodda hamma SQL `?` belgili parametrlar bilan yoziladi (SQLite uslubi)."""


def to_pg(sql: str) -> str:
    """`?` -> `%s`, matn ichidagi va boshqa `%` -> `%%` (psycopg shuni talab qiladi). Qo'shtirnoqdagi matn ichidagi `?` tegilmaydi."""
    out: list[str] = []
    in_str = False
    i = 0
    while i < len(sql):
        ch = sql[i]
        if in_str:
            out.append("%%" if ch == "%" else ch)
            if ch == "'":
                if i + 1 < len(sql) and sql[i + 1] == "'":      # '' — matn ichidagi qo'shtirnoq
                    out.append("'")
                    i += 1
                else:
                    in_str = False
        elif ch == "'":
            in_str = True
            out.append(ch)
        elif ch == "?":
            out.append("%s")
        elif ch == "%":
            out.append("%%")
        else:
            out.append(ch)
        i += 1
    return "".join(out)
