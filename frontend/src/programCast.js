// Ordered like the Nijigasaki event calendar, including both Setsuna voice actors.
// `id` is the stable key used by the ICS feed (7 = Setsuna, 14 = her former voice actor).
export const NIJIGASAKI_CAST = [
  { id: 1, name: "大西亜玖璃", aliases: ["大西亚玖璃"], color: "#ed7d95" },
  { id: 2, name: "相良茉優", aliases: ["相良茉优"], color: "#e7d600" },
  { id: 3, name: "前田佳織里", aliases: ["前田佳织里"], color: "#01b7ed" },
  { id: 4, name: "久保田未夢", aliases: ["久保田未梦"], color: "#485ec6" },
  { id: 5, name: "村上奈津実", aliases: ["村上奈津实"], color: "#ff5800" },
  { id: 6, name: "鬼頭明里", aliases: ["鬼头明里"], color: "#a664a0" },
  { id: 14, name: "楠木ともり", aliases: ["楠木灯"], color: "#d81c2f" },
  { id: 7, name: "林鼓子", aliases: [], color: "#d81c2f" },
  { id: 8, name: "指出毬亜", aliases: ["指出毬亚"], color: "#84c36e" },
  { id: 9, name: "田中ちえ美", aliases: ["田中千惠美"], color: "#9ca5b9" },
  { id: 10, name: "小泉萌香", aliases: [], color: "#37b484" },
  { id: 11, name: "内田秀", aliases: [], color: "#a99e98" },
  { id: 12, name: "法元明菜", aliases: [], color: "#f8c8c4" },
  { id: 13, name: "矢野妃菜喜", aliases: [], color: "#2f2f2f" },
];

export function castMemberMatches(people = [], member) {
  if (!member) return false;
  const names = new Set((Array.isArray(people) ? people : [])
    .map(person => String(person || "").trim())
    .filter(Boolean));
  return [member.name, ...member.aliases].some(name => names.has(name));
}

export function castColorSegments(people = [], absent = []) {
  return NIJIGASAKI_CAST.filter(member => castMemberMatches(people, member) && !castMemberMatches(absent, member));
}
