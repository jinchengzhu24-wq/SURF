using System.Collections;
using System.Collections.Generic;
using UnityEngine;
using UnityEngine.Tilemaps;

public class LevelLoader : MonoBehaviour
{
    [Header("Level Data")]
    public LevelData levelData;
    public LevelManager levelManager;

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

    private void Awake()
    {
        if (levelManager == null)
        {
            levelManager = FindObjectOfType<LevelManager>();
        }

        LoadLevel();
    }

    private void LoadLevel()
    {
        if (levelData == null || levelData.rows == null)
        {
            return;
        }

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
            SetTile(waterTilemap, GetWaterTile(x, y), cellPosition);
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

        return Instantiate(prefab, position, Quaternion.identity, levelRoot);
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
        if (IsGround(tile))
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

    private bool IsGround(int x, int y)
    {
        return IsGround(GetMapTile(x, y));
    }

    private bool IsGround(char tile)
    {
        return tile == ' ' || tile == 'p' || tile == 's' || tile == 't';
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
}
