def format_selected_summary(selected_items, product_names):
    if not selected_items:
        return ""
    lines = []
    for pid, qty in selected_items.items():
        name = product_names.get(pid, f"Товар {pid}")
        lines.append(f"{name} – {qty} упаковок")
    if len(lines) == 1:
        items_lines = lines[0] + "."
    else:
        items_lines = "\n".join([f"{line}," for line in lines[:-1]] + [f"{lines[-1]}."])
    return f"Вы продали:\n{items_lines}\n\nВерно?"
