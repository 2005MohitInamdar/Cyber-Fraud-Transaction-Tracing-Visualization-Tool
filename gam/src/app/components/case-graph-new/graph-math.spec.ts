import { describe, expect, it } from 'vitest';
import { edgePath, fitView, formatCompactInr, screenToGraph, zoomAt } from './graph-math';

describe('transaction graph math', () => {
  it('formats compact Indian currency amounts', () => {
    expect([null, 500, 999.6, 1000, 45000, 123456, 12345678].map(formatCompactInr))
      .toEqual(['—', '₹500', '₹1000', '₹1K', '₹45K', '₹1.2L', '₹1.2Cr']);
  });

  it('keeps the zoom anchor fixed and clamps the zoom level', () => {
    const before = { k: 1, tx: 10, ty: 20 };
    const graphPoint = screenToGraph(before, 100, 90);
    const after = zoomAt(before, 2, 100, 90, .2, 4);
    expect(screenToGraph(after, 100, 90)).toEqual(graphPoint);
    expect(zoomAt(before, 99, 100, 90, .2, 4).k).toBe(4);
  });

  it('fits and centres graph bounds without enlarging them', () => {
    const view = fitView({ minX: 0, minY: 0, maxX: 100, maxY: 100 }, 200, 200, 40);
    expect(view.k).toBeLessThanOrEqual(1);
    expect(view.tx).toBe(50);
    expect(view.ty).toBe(50);
  });

  it('starts and ends an edge on its circle borders in either direction', () => {
    expect(edgePath({ x: 0, y: 0 }, 10, { x: 100, y: 0 }, 20)).toContain('M 10,0');
    expect(edgePath({ x: 100, y: 0 }, 10, { x: 0, y: 0 }, 20)).toContain('M 90,0');
  });
});
