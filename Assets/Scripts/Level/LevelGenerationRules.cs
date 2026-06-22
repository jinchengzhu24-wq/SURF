using UnityEngine;

// 当前关卡生成规则：
// - 生成的地图使用固定宽度和高度。
// - 地图最外圈必须是墙。
// - 可游玩区域位于外圈墙的内部。
// - 每张地图必须且只能有一个玩家起点。
// - 每张地图必须生成 boxCount 个箱子。
// - 终点数量必须和箱子数量一致。
// - 水面必须按矩形区域生成，不能是零散随机格子。
// - 合法水面矩形至少需要满足 2x3 或 3x2。
// - 水面矩形必须完整位于可游玩区域内。
// - 生成器最多尝试 maxGenerateAttempts 次，避免无限循环。
// - 最终生成的地图必须通过 LevelSolver 检测，确认有解后才接受。
public class LevelGenerationRules : MonoBehaviour
{
    [Header("Map Size")]
    public int width = 12;
    public int height = 10;

    [Header("Objects")]
    public int boxCount = 2;

    [Header("Water")]
    public int minWaterWidth = 2;
    public int minWaterHeight = 2;
    public int maxWaterWidth = 4;
    public int maxWaterHeight = 4;
    public int maxWaterAreas = 2;

    [Header("Search")]
    public int maxGenerateAttempts = 100;

    [Header("Reverse Generation")]
    public int minReversePulls = 8;
    public int maxReversePulls = 24;
    public int maxReverseStepAttempts = 200;
    public bool useFixedSeed;
    public int seed;

    public bool IsValid()
    {
        return width >= 5
            && height >= 5
            && boxCount > 0
            && minWaterWidth > 0
            && minWaterHeight > 0
            && maxWaterWidth >= minWaterWidth
            && maxWaterHeight >= minWaterHeight
            && maxWaterAreas >= 0
            && maxGenerateAttempts > 0
            && minReversePulls >= 0
            && maxReversePulls >= minReversePulls
            && maxReverseStepAttempts > 0;
    }

    public bool IsInsideMap(Vector2Int position)
    {
        return position.x >= 0
            && position.x < width
            && position.y >= 0
            && position.y < height;
    }

    public bool IsInsidePlayableArea(Vector2Int position)
    {
        return position.x > 0
            && position.x < width - 1
            && position.y > 0
            && position.y < height - 1;
    }

    public bool IsValidWaterRectSize(int rectWidth, int rectHeight)
    {
        bool tallEnough = rectWidth >= 2 && rectHeight >= 3;
        bool wideEnough = rectWidth >= 3 && rectHeight >= 2;

        return tallEnough || wideEnough;
    }

    public bool IsValidWaterRect(Vector2Int origin, Vector2Int size)
    {
        if (!IsValidWaterRectSize(size.x, size.y))
        {
            return false;
        }

        if (size.x < minWaterWidth || size.x > maxWaterWidth)
        {
            return false;
        }

        if (size.y < minWaterHeight || size.y > maxWaterHeight)
        {
            return false;
        }

        Vector2Int min = origin;
        Vector2Int max = new Vector2Int(origin.x + size.x - 1, origin.y + size.y - 1);

        return IsInsidePlayableArea(min) && IsInsidePlayableArea(max);
    }

    public int GetRandomWaterWidth()
    {
        return Random.Range(minWaterWidth, maxWaterWidth + 1);
    }

    public int GetRandomWaterHeight()
    {
        return Random.Range(minWaterHeight, maxWaterHeight + 1);
    }

    public Vector2Int GetRandomPlayablePosition()
    {
        return new Vector2Int(
            Random.Range(1, width - 1),
            Random.Range(1, height - 1)
        );
    }
}
