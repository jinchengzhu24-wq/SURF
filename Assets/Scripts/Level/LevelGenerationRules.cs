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
    public int minWaterAreas = 1;
    public int maxWaterAreas = 2;

    [Header("Search")]
    public int maxGenerateAttempts = 300;

    [Header("Candidate Quality")]
    public int algorithmCandidateSampleCount = 10;
    public int algorithmMinimumQualityScore = 220;
    public int algorithmPreferredMinSolutionSteps = 22;
    public int algorithmPreferredMinPushes = 8;
    public int algorithmPreferredMinReversePulls = 18;
    public int algorithmMinimumObstacleInfluence = 2;
    public int llmCandidateSampleCount = 2;
    public int llmMinimumQualityScore = 220;
    public int llmMinimumWaterTiles = 4;
    public int llmMinimumSurroundedWalls = 1;
    public int llmMaxPlanRetries = 2;
    [Range(0, 100)]
    public int recentStructureSimilarityThreshold = 86;

    [Header("Outer Walls")]
    public bool enableIrregularOuterWalls = true;

    [Header("Wall Obstacles")]
    public int minWallObstacleBlocks = 1;
    public int maxWallObstacleBlocks = 3;

    [Header("Difficulty")]
    public int minSolutionSteps = 12;
    public int maxSolutionSteps = 35;
    public int minPushes = 0;
    public int maxPushes = 999;

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
            && minWaterAreas >= 0
            && maxWaterAreas >= minWaterAreas
            && maxWaterAreas >= 0
            && maxGenerateAttempts > 0
            && algorithmCandidateSampleCount > 0
            && algorithmMinimumQualityScore >= 0
            && algorithmPreferredMinSolutionSteps >= 0
            && algorithmPreferredMinPushes >= 0
            && algorithmPreferredMinReversePulls >= 0
            && algorithmMinimumObstacleInfluence >= 0
            && llmCandidateSampleCount > 0
            && llmMinimumQualityScore >= 0
            && llmMinimumWaterTiles >= 0
            && llmMinimumSurroundedWalls >= 0
            && llmMaxPlanRetries > 0
            && recentStructureSimilarityThreshold >= 0
            && recentStructureSimilarityThreshold <= 100
            && minWallObstacleBlocks >= 0
            && maxWallObstacleBlocks >= minWallObstacleBlocks
            && minSolutionSteps >= 0
            && maxSolutionSteps >= minSolutionSteps
            && minPushes >= 0
            && maxPushes >= minPushes
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

}
