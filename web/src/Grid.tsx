import { type ColumnDef, flexRender, getCoreRowModel, getSortedRowModel, type SortingState, useReactTable } from "@tanstack/react-table";
import { useState } from "react";

interface Props<T> {
  rows: T[];
  columns: ColumnDef<T, any>[];
  rowClass?: (row: T) => string;
  rowId?: (row: T) => string;
  onRowClick?: (row: T) => void;
}

/** Dense sortable grid (TanStack Table). ponytail: no row virtualisation; add @tanstack/react-virtual past ~5k rows. */
export function Grid<T>({ rows, columns, rowClass, rowId, onRowClick }: Props<T>) {
  const [sorting, setSorting] = useState<SortingState>([]);
  const table = useReactTable({
    data: rows, columns, state: { sorting }, onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(), getSortedRowModel: getSortedRowModel(),
  });
  return (
    <div className="grid">
      <table>
        <thead>
          {table.getHeaderGroups().map((hg) => (
            <tr key={hg.id}>
              {hg.headers.map((h) => (
                <th key={h.id} className={(h.column.columnDef.meta as { num?: boolean } | undefined)?.num ? "num" : ""}
                    aria-sort={h.column.getIsSorted() === "asc" ? "ascending" : h.column.getIsSorted() === "desc" ? "descending" : "none"}>
                  <button onClick={h.column.getToggleSortingHandler()}>
                    {flexRender(h.column.columnDef.header, h.getContext())}
                    {{ asc: " ▲", desc: " ▼" }[h.column.getIsSorted() as string] ?? ""}
                  </button>
                </th>
              ))}
            </tr>
          ))}
        </thead>
        <tbody>
          {table.getRowModel().rows.map((r) => (
            <tr key={r.id} id={rowId?.(r.original)} className={rowClass?.(r.original)} tabIndex={onRowClick ? 0 : undefined}
                onClick={() => onRowClick?.(r.original)} onKeyDown={(e) => e.key === "Enter" && onRowClick?.(r.original)}>
              {r.getVisibleCells().map((c) => (
                <td key={c.id} className={(c.column.columnDef.meta as { num?: boolean } | undefined)?.num ? "num" : ""}>
                  {flexRender(c.column.columnDef.cell, c.getContext())}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {rows.length === 0 && <p className="empty">Nothing here.</p>}
    </div>
  );
}
