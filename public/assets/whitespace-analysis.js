(function (root) {
  'use strict';

  const clamp = value => Math.max(0, Math.min(1, Number(value) || 0));
  const ratio = (value, maximum) => maximum > 0 ? clamp(value / maximum) : 0;
  const level = score => score >= .67 ? '高' : score >= .4 ? '中' : '低';

  function analyze(input) {
    const techs = (input.techs || []).map(String);
    const problems = (input.problems || []).map(String);
    const tech = String(input.tech);
    const problem = String(input.problem);
    const counts = input.counts instanceof Map ? input.counts : new Map(Object.entries(input.counts || {}));
    const countAt = (techId, problemId) => Number(counts.get(`${techId}|${problemId}`) || 0);
    if (countAt(tech, problem) !== 0) return null;

    const rowTotals = new Map(problems.map(problemId => [
      problemId,
      techs.reduce((sum, techId) => sum + countAt(techId, problemId), 0),
    ]));
    const columnTotals = new Map(techs.map(techId => [
      techId,
      problems.reduce((sum, problemId) => sum + countAt(techId, problemId), 0),
    ]));
    const techIndex = techs.indexOf(tech);
    const problemIndex = problems.indexOf(problem);
    const neighborCoordinates = [
      [techIndex - 1, problemIndex], [techIndex + 1, problemIndex],
      [techIndex, problemIndex - 1], [techIndex, problemIndex + 1],
    ].filter(([x, y]) => x >= 0 && y >= 0 && x < techs.length && y < problems.length);
    const occupiedNeighbors = neighborCoordinates.filter(([x, y]) => countAt(techs[x], problems[y]) > 0);

    const queue = [[techIndex, problemIndex]];
    const visited = new Set();
    while (queue.length) {
      const [x, y] = queue.shift();
      const key = `${x}|${y}`;
      if (visited.has(key) || x < 0 || y < 0 || x >= techs.length || y >= problems.length) continue;
      if (countAt(techs[x], problems[y]) !== 0) continue;
      visited.add(key);
      queue.push([x - 1, y], [x + 1, y], [x, y - 1], [x, y + 1]);
    }

    const rowTotal = rowTotals.get(problem) || 0;
    const columnTotal = columnTotals.get(tech) || 0;
    const demand = ratio(rowTotal, Math.max(0, ...rowTotals.values()));
    const capability = ratio(columnTotal, Math.max(0, ...columnTotals.values()));
    const adjacency = neighborCoordinates.length ? occupiedNeighbors.length / neighborCoordinates.length : 0;
    const technologyFit = clamp(input.technologyFit);
    const problemFit = clamp(input.problemFit);
    const strategicFit = (technologyFit + problemFit) / 2;
    const opportunityScore = .3 * demand + .25 * capability + .25 * strategicFit + .2 * adjacency;
    const bridgeScore = .4 * capability + .3 * demand + .3 * adjacency;

    let pattern = '孤立した空白';
    let patternMeaning = '周囲には文献があり、既存要素の組合せが見落とされている可能性があります。';
    if (visited.size >= Math.max(4, Math.ceil(techs.length * problems.length * .2))) {
      pattern = '大きな空白領域';
      patternMeaning = '偶然の取りこぼしより、技術障壁・需要不足・分類軸の不整合を先に疑うべき形です。';
    } else if (visited.size > 1) {
      pattern = '連続した空白領域';
      patternMeaning = '単独セルではなく、共通の制約や検索範囲の偏りが影響している可能性があります。';
    }

    return {
      opportunityScore,
      opportunityLevel: level(opportunityScore),
      bridgeScore,
      bridgeLevel: level(bridgeScore),
      strategicFit,
      strategicFitLevel: level(strategicFit),
      rowTotal,
      columnTotal,
      occupiedNeighbors: occupiedNeighbors.length,
      neighborCount: neighborCoordinates.length,
      blockSize: visited.size,
      pattern,
      patternMeaning,
    };
  }

  root.PatentWhitespace = { analyze };
  if (typeof module !== 'undefined' && module.exports) module.exports = root.PatentWhitespace;
}(typeof window !== 'undefined' ? window : globalThis));
