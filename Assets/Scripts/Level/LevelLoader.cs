using System.Collections;
using System.Collections.Generic;
using UnityEngine;
using UnityEngine.Tilemaps;

// 当前 LevelData 字符含义：
// - p：玩家起点，生成 Player prefab，并且该格也视为地面。
// - s：箱子起点，生成 Start prefab 和 Box prefab，并且该格也视为地面。
// - t：箱子终点，生成 Target prefab，并且该格也视为地面。
// - .：普通地面。
// - #：墙体。
// - @：水面。
// - 空格：空白区域，不生成任何瓦片或物体。
//
// 当前地面瓦片类型：
// - groundTile：普通地面。
// - groundRightWallTile：右侧是墙时使用的地面。
//
// 当前墙体瓦片类型：
// - wallTile：普通墙。
// - wallVerticalTile：上方或下方有墙时使用的墙。
// - wallRightAndRightDownTile：右侧和右下都有墙时使用的墙。
// - wallSurroundedTile：周围八个方向都有瓦片，且下方不是水时使用的特殊墙。
//
// 当前水面瓦片类型：
// - waterTile：普通水面。
// - waterTopGroundTile：上方是地面时使用的水面。
// - waterRightWallTile：右侧是墙时使用的水面。
// - waterTopGroundRightWallTile：上方是地面且右侧是墙时使用的水面。
public class LevelLoader : MonoBehaviour
{
    [Header("Level Data")]
    public LevelData levelData;
    public LevelManager levelManager;

    [Header("Generation")]
    public LevelGenerator levelGenerator;
    public LLMLevelDesignClient llmClient;
    public bool generateBeforeLoad = true;
    public bool useLLMPlan;
    public bool useCachedLLMPlan = true;
    public float cachedPlanWaitSeconds = 1f;

    [Header("Prefabs")]
    public GameObject playerPrefab;
    public GameObject boxPrefab;
    public GameObject startPrefab;
    public GameObject targetPrefab;

    [Header("Tilemaps")]
    public Tilemap groundTilemap;
    public Tilemap wallTilemap;
    public Tilemap waterTilemap;
    public TileBase groundTile;
    public TileBase groundRightWallTile;
    public TileBase wallTile;
    public TileBase wallVerticalTile;
    public TileBase wallRightAndRightDownTile;
    public TileBase wallSurroundedTile;
    public TileBase waterTile;
    public TileBase waterTopGroundTile;
    public TileBase waterRightWallTile;
    public TileBase waterTopGroundRightWallTile;

    [Header("Settings")]
    public float cellSize = 1f;
    public Transform levelRoot;
    public bool clearTilemapsOnLoad = true;
    public bool centerMap = true;
    public Vector2Int extraCellOffset;

    private readonly List<GameObject> spawnedObjects = new List<GameObject>();
    private bool currentLoadUsedLLMPlan;

    private void Awake()
    {
        if (levelManager == null)
        {
            levelManager = FindObjectOfType<LevelManager>();
        }

        ResolveGenerationReferences();

        if (generateBeforeLoad && useLLMPlan)
        {
            StartCoroutine(GenerateAndLoadWithLLMPlanRoutine());
        }
        else
        {
            bool generatedLevel = GenerateLevelIfNeeded();
            LoadLevel();
            NotifyGeneratedLevelIfNeeded(generatedLevel);
        }
    }

    private bool GenerateLevelIfNeeded()
    {
        if (!generateBeforeLoad || levelGenerator == null)
        {
            return false;
        }

        return GenerateLevel();
    }

    public void GenerateAndReload()
    {
        bool generatedLevel = GenerateLevel();
        LoadLevel();
        NotifyGeneratedLevelIfNeeded(generatedLevel);
    }

    [ContextMenu("Generate With LLM Plan")]
    public void GenerateWithLLMPlan()
    {
        StartCoroutine(GenerateAndReloadWithLLMPlanRoutine());
    }

    public IEnumerator GenerateAndReloadWithLLMPlanRoutine()
    {
        yield return RequestAndApplyLLMPlan();
        GenerateAndReload();
    }

    private IEnumerator GenerateAndLoadWithLLMPlanRoutine()
    {
        yield return RequestAndApplyLLMPlan();
        bool generatedLevel = GenerateLevel();
        LoadLevel();
        NotifyGeneratedLevelIfNeeded(generatedLevel);
    }

    private void NotifyGeneratedLevelIfNeeded(bool generatedLevel)
    {
        if (!generatedLevel)
        {
            return;
        }

        if (levelManager == null)
        {
            levelManager = FindObjectOfType<LevelManager>();
        }

        if (levelManager != null)
        {
            levelManager.RegisterGeneratedLevel();
        }
    }

    private bool GenerateLevel()
    {
        ResolveGenerationReferences();

        if (levelGenerator == null)
        {
            return false;
        }

        if (!useLLMPlan)
        {
            currentLoadUsedLLMPlan = false;
        }

        if (levelData != null)
        {
            levelGenerator.levelData = levelData;
        }

        levelGenerator.levelLoader = this;

        if (levelGenerator.Generate() && levelGenerator.levelData != null)
        {
            levelData = levelGenerator.levelData;
            return true;
        }

        return false;
    }

    private IEnumerator RequestAndApplyLLMPlan()
    {
        ResolveGenerationReferences();
        currentLoadUsedLLMPlan = false;

        LevelDesignPlan plan = null;

        if (useCachedLLMPlan)
        {
            yield return RequestCachedLLMPlan(result => plan = result);
        }

        if (plan != null)
        {
            ApplyLLMPlan(plan);
            yield break;
        }

        if (llmClient == null)
        {
            Debug.LogWarning("LevelLoader: LLM plan client is missing. Using local generation rules fallback.");
            yield break;
        }

        yield return llmClient.RequestPlan(result => plan = result);

        if (plan == null)
        {
            Debug.LogWarning("LevelLoader: LLM plan request failed. Using local generation rules fallback.");
            yield break;
        }

        ApplyLLMPlan(plan);
    }

    private IEnumerator RequestCachedLLMPlan(System.Action<LevelDesignPlan> onComplete)
    {
        LLMLevelPlanCache cache = LLMLevelPlanCache.Instance;

        if (cache == null)
        {
            Debug.Log("LevelLoader: LLM plan cache miss because cache is missing.");
            onComplete?.Invoke(null);
            yield break;
        }

        if (cache.TryTakePlan(out LevelDesignPlan plan))
        {
            onComplete?.Invoke(plan);
            yield break;
        }

        cache.EnsurePlanBuffer();

        if (cachedPlanWaitSeconds > 0f && cache.IsRequesting)
        {
            yield return cache.WaitForPlan(cachedPlanWaitSeconds, result => plan = result);

            if (plan != null)
            {
                onComplete?.Invoke(plan);
                yield break;
            }
        }

        Debug.Log("LevelLoader: LLM plan cache miss. Requesting a plan directly.");
        onComplete?.Invoke(null);
    }

    private void ApplyLLMPlan(LevelDesignPlan plan)
    {
        if (levelGenerator == null)
        {
            Debug.LogWarning("LevelLoader: Cannot apply LLM plan because LevelGenerator is missing.");
            return;
        }

        levelGenerator.ApplyPlan(plan);
        currentLoadUsedLLMPlan = true;
    }

    public string GetCurrentLevelSource()
    {
        if (!generateBeforeLoad || levelGenerator == null)
        {
            return "Static";
        }

        if (useLLMPlan)
        {
            return currentLoadUsedLLMPlan ? "LLMGuided" : "Fallback";
        }

        return "Algorithm";
    }

    private void ResolveGenerationReferences()
    {
        if (levelManager == null)
        {
            levelManager = FindObjectOfType<LevelManager>();
        }

        if (levelGenerator == null)
        {
            levelGenerator = FindObjectOfType<LevelGenerator>();
        }

        if (llmClient == null)
        {
            llmClient = FindObjectOfType<LLMLevelDesignClient>();
        }
    }

    public void LoadLevel()
    {
        if (levelData == null || levelData.rows == null)
        {
            return;
        }

        ClearSpawnedObjects();

        if (clearTilemapsOnLoad)
        {
            ClearTilemaps();
        }

        int mapWidth = GetMapWidth();
        int mapHeight = levelData.rows.Length;

        for (int y = 0; y < levelData.rows.Length; y++)
        {
            string row = levelData.rows[y];

            for (int x = 0; x < row.Length; x++)
            {
                char tile = row[x];
                Vector3Int cellPosition = GetCellPosition(x, y, mapWidth, mapHeight);
                Vector3 position = GetWorldPosition(cellPosition);

                SetGroundTile(tile, x, y, cellPosition);
                SpawnTile(tile, x, y, cellPosition, position);
            }
        }

        if (levelManager != null)
        {
            levelManager.ResetLevelState();
        }

        LevelStudyRecorder.RecordLevelStarted(this);
    }

    private Vector3Int GetCellPosition(int x, int y, int mapWidth, int mapHeight)
    {
        int cellX = x;
        int cellY = -y;

        if (centerMap)
        {
            cellX -= mapWidth / 2;
            cellY += mapHeight / 2;
        }

        cellX += extraCellOffset.x;
        cellY += extraCellOffset.y;

        return new Vector3Int(cellX, cellY, 0);
    }

    private int GetMapWidth()
    {
        int width = 0;

        for (int i = 0; i < levelData.rows.Length; i++)
        {
            if (levelData.rows[i].Length > width)
            {
                width = levelData.rows[i].Length;
            }
        }

        return width;
    }

    private void SpawnTile(char tile, int x, int y, Vector3Int cellPosition, Vector3 position)
    {
        if (tile == 'p')
        {
            SpawnPlayer(position);
        }
        else if (tile == 's')
        {
            SpawnBoxStart(position);
        }
        else if (tile == 't')
        {
            Spawn(targetPrefab, position);
        }
        else if (tile == '@')
        {
            if (!IsWall(x, y - 1))
            {
                SetTile(waterTilemap, GetWaterTile(x, y), cellPosition);
            }
        }
        else if (tile == '#')
        {
            SetTile(wallTilemap, GetWallTile(x, y), cellPosition);
        }
    }

    private void SpawnPlayer(Vector3 position)
    {
        GameObject player = Spawn(playerPrefab, position);

        if (player != null && levelManager != null && levelManager.anim == null)
        {
            levelManager.anim = player.GetComponent<PlayerAnimation>();
        }
    }

    private void SpawnBoxStart(Vector3 position)
    {
        GameObject startObject = Spawn(startPrefab, position);
        GameObject boxObject = Spawn(boxPrefab, position);

        if (boxObject == null)
        {
            return;
        }

        Box box = boxObject.GetComponent<Box>();

        if (box == null)
        {
            return;
        }

        if (startObject != null)
        {
            box.start = startObject.transform;
        }

        box.LM = levelManager;
    }

    private GameObject Spawn(GameObject prefab, Vector3 position)
    {
        if (prefab == null)
        {
            return null;
        }

        GameObject instance = Instantiate(prefab, position, Quaternion.identity, levelRoot);
        spawnedObjects.Add(instance);
        return instance;
    }

    private void SetTile(Tilemap tilemap, TileBase tile, Vector3Int cellPosition)
    {
        if (tilemap == null || tile == null)
        {
            return;
        }

        tilemap.SetTile(cellPosition, tile);
    }

    private void SetGroundTile(char tile, int x, int y, Vector3Int cellPosition)
    {
        if (IsSurroundedWall(x, y))
        {
            SetTile(groundTilemap, groundTile, cellPosition);
            return;
        }

        if (IsGround(tile))
        {
            SetTile(groundTilemap, GetGroundTile(x, y), cellPosition);
            return;
        }

        if (tile == '@' && IsWall(x, y - 1))
        {
            SetTile(groundTilemap, GetGroundTile(x, y), cellPosition);
        }
    }

    private TileBase GetGroundTile(int x, int y)
    {
        if (IsWall(x + 1, y))
        {
            return GetFallbackTile(groundRightWallTile, groundTile);
        }

        return groundTile;
    }

    private TileBase GetWaterTile(int x, int y)
    {
        bool topGround = IsGround(x, y - 1);
        bool rightWall = IsWall(x + 1, y);

        if (topGround && rightWall)
        {
            return GetFallbackTile(waterTopGroundRightWallTile, waterTile);
        }

        if (topGround)
        {
            return GetFallbackTile(waterTopGroundTile, waterTile);
        }

        if (rightWall)
        {
            return GetFallbackTile(waterRightWallTile, waterTile);
        }

        return waterTile;
    }

    private TileBase GetWallTile(int x, int y)
    {
        if (IsSurroundedWall(x, y))
        {
            return GetFallbackTile(wallSurroundedTile, wallTile);
        }

        if (IsWall(x, y - 1) && (IsWall(x - 1, y) || IsWall(x + 1, y)))
        {
            return wallTile;
        }

        if (IsWall(x + 1, y) && IsWall(x + 1, y + 1))
        {
            return GetFallbackTile(wallRightAndRightDownTile, wallTile);
        }

        if (IsWall(x, y - 1) || IsWall(x, y + 1))
        {
            return GetFallbackTile(wallVerticalTile, wallTile);
        }

        return wallTile;
    }

    private TileBase GetFallbackTile(TileBase tile, TileBase fallbackTile)
    {
        return tile != null ? tile : fallbackTile;
    }

    private bool IsWall(int x, int y)
    {
        return GetMapTile(x, y) == '#';
    }

    private bool IsWater(int x, int y)
    {
        return GetMapTile(x, y) == '@';
    }

    private bool HasTile(int x, int y)
    {
        char tile = GetMapTile(x, y);
        return tile != '\0' && tile != ' ';
    }

    private bool HasTilesAround(int x, int y)
    {
        return HasTile(x - 1, y - 1)
            && HasTile(x, y - 1)
            && HasTile(x + 1, y - 1)
            && HasTile(x - 1, y)
            && HasTile(x + 1, y)
            && HasTile(x - 1, y + 1)
            && HasTile(x, y + 1)
            && HasTile(x + 1, y + 1);
    }

    private bool IsSurroundedWall(int x, int y)
    {
        return IsWall(x, y) && HasTilesAround(x, y) && !IsWater(x, y + 1);
    }

    private bool IsGround(int x, int y)
    {
        return IsGround(GetMapTile(x, y));
    }

    private bool IsGround(char tile)
    {
        return tile == '.' || tile == 'p' || tile == 's' || tile == 't';
    }

    private char GetMapTile(int x, int y)
    {
        if (levelData == null || levelData.rows == null)
        {
            return '\0';
        }

        if (y < 0 || y >= levelData.rows.Length)
        {
            return '\0';
        }

        string row = levelData.rows[y];

        if (x < 0 || x >= row.Length)
        {
            return '\0';
        }

        return row[x];
    }

    private Vector3 GetWorldPosition(Vector3Int cellPosition)
    {
        Tilemap referenceTilemap = groundTilemap;

        if (referenceTilemap == null)
        {
            referenceTilemap = wallTilemap;
        }

        if (referenceTilemap == null)
        {
            referenceTilemap = waterTilemap;
        }

        if (referenceTilemap != null)
        {
            return referenceTilemap.GetCellCenterWorld(cellPosition);
        }

        return new Vector3(cellPosition.x * cellSize, cellPosition.y * cellSize, 0);
    }

    private void ClearTilemaps()
    {
        if (groundTilemap != null)
        {
            groundTilemap.ClearAllTiles();
        }

        if (wallTilemap != null)
        {
            wallTilemap.ClearAllTiles();
        }

        if (waterTilemap != null && waterTilemap != groundTilemap && waterTilemap != wallTilemap)
        {
            waterTilemap.ClearAllTiles();
        }
    }

    private void ClearSpawnedObjects()
    {
        if (levelManager != null)
        {
            levelManager.anim = null;
        }

        for (int i = spawnedObjects.Count - 1; i >= 0; i--)
        {
            GameObject spawnedObject = spawnedObjects[i];

            if (spawnedObject == null)
            {
                continue;
            }

            spawnedObject.SetActive(false);

            if (Application.isPlaying)
            {
                Destroy(spawnedObject);
            }
            else
            {
                DestroyImmediate(spawnedObject);
            }
        }

        spawnedObjects.Clear();
    }
}
