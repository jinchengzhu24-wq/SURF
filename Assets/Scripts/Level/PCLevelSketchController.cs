using System.Collections.Generic;
using UnityEngine;
using UnityEngine.EventSystems;
using UnityEngine.Tilemaps;
using UnityEngine.UI;

public class PCLevelSketchController : MonoBehaviour
{
    public enum SketchBrush
    {
        Wall,
        BoxStart,
        Target,
        Eraser
    }

    [Header("Scene References")]
    [SerializeField] private Camera sceneCamera;
    [SerializeField] private Tilemap baseTilemap;
    [SerializeField] private Tilemap wallTilemap;
    [SerializeField] private Tilemap markerTilemap;
    [SerializeField] private TileBase wallTile;
    [SerializeField] private TileBase startTile;
    [SerializeField] private TileBase targetTile;

    [Header("Palette UI")]
    [SerializeField] private Button wallButton;
    [SerializeField] private Button startButton;
    [SerializeField] private Button targetButton;
    [SerializeField] private Button eraserButton;
    [SerializeField] private Text countText;
    [SerializeField] private Text statusText;
    [SerializeField] private Button checkButton;
    [SerializeField] private Button submitButton;
    [SerializeField] private Text warningText;

    [Header("Editable Area")]
    [SerializeField] private Vector2Int mapSize = new Vector2Int(12, 10);
    [SerializeField] private Vector3Int bottomLeftCell = new Vector3Int(-6, -4, 0);
    [SerializeField] private int maximumStartCount = 2;
    [SerializeField] private int maximumTargetCount = 2;
    [SerializeField] private int minimumActivityArea = 48;

    private char[,] sketchCells;
    private SketchBrush selectedBrush = SketchBrush.Wall;
    private int startCount;
    private int targetCount;
    private Vector3Int lastPaintedCell;
    private bool hasLastPaintedCell;
    private bool currentStrokeStartedOverUi;

    private static readonly Vector2Int[] cardinalDirections =
    {
        Vector2Int.up,
        Vector2Int.down,
        Vector2Int.left,
        Vector2Int.right
    };

    public SketchBrush SelectedBrush => selectedBrush;
    public int StartCount => startCount;
    public int TargetCount => targetCount;
    public int ActivityArea => CountLargestEnclosedGroundArea();

    private void Awake()
    {
        sketchCells = new char[mapSize.x, mapSize.y];
        ClearEditableTilemaps();
        UpdateCountText();
        SetStatus("Wall selected.");
        SetSubmitInteractable(false);
        ClearWarning();
        ValidateSceneReferences();
    }

    private void Update()
    {
        if (Input.GetMouseButtonDown(0))
        {
            hasLastPaintedCell = false;
            currentStrokeStartedOverUi = IsPointerOverUi();
        }

        if (Input.GetMouseButtonUp(0))
        {
            hasLastPaintedCell = false;
            currentStrokeStartedOverUi = false;
            return;
        }

        if (!Input.GetMouseButton(0)
            || currentStrokeStartedOverUi
            || IsPointerOverUi())
        {
            return;
        }

        TryPaintAtScreenPosition(Input.mousePosition);
    }

    private bool IsPointerOverUi()
    {
        return EventSystem.current != null
            && EventSystem.current.IsPointerOverGameObject();
    }

    public void SelectWallBrush()
    {
        selectedBrush = SketchBrush.Wall;
        SetStatus("Wall selected.");
    }

    public void SelectStartBrush()
    {
        selectedBrush = SketchBrush.BoxStart;
        SetStatus("Box start selected.");
    }

    public void SelectTargetBrush()
    {
        selectedBrush = SketchBrush.Target;
        SetStatus("Target selected.");
    }

    public void SelectEraserBrush()
    {
        selectedBrush = SketchBrush.Eraser;
        SetStatus("Eraser selected.");
    }

    public string[] GetSketchRows()
    {
        string[] rows = new string[mapSize.y];

        for (int row = 0; row < mapSize.y; row++)
        {
            char[] rowCharacters = new char[mapSize.x];
            int localY = mapSize.y - 1 - row;

            for (int x = 0; x < mapSize.x; x++)
            {
                char tile = sketchCells[x, localY];
                rowCharacters[x] = tile == '\0' ? LevelData.Empty : tile;
            }

            rows[row] = new string(rowCharacters);
        }

        return rows;
    }

    public bool ValidateSketch()
    {
        return TryValidateSketch(out _);
    }

    public void CheckSketch()
    {
        bool isValid = TryValidateSketch(out string message);
        SetSubmitInteractable(isValid);

        if (isValid)
        {
            ShowWarning("Pass", new Color(0.12f, 0.72f, 0.25f, 1f));
        }
        else
        {
            ShowWarning(message, new Color(0.82f, 0.08f, 0.12f, 1f));
        }
    }

    public void SubmitSketch()
    {
        bool isValid = TryValidateSketch(out string message);

        if (!isValid)
        {
            SetSubmitInteractable(false);
            ShowWarning(message, new Color(0.82f, 0.08f, 0.12f, 1f));
            return;
        }

        ShowWarning("Pass", new Color(0.12f, 0.72f, 0.25f, 1f));
    }

    public bool TryValidateSketch(out string message)
    {
        if (startCount != targetCount)
        {
            message = "Box start and target counts must match.";
            return false;
        }

        if (!AreMapEdgesClosed())
        {
            message = "Every map edge cell must be a wall.";
            return false;
        }

        int activityArea = CountLargestEnclosedGroundArea();

        if (!AreAllGroundCellsSingleConnectedRegion())
        {
            message = "Walls must enclose one connected activity area.";
            return false;
        }

        if (activityArea < minimumActivityArea)
        {
            message = "Activity area needs at least "
                + minimumActivityArea
                + " cells (current "
                + activityArea
                + ").";
            return false;
        }

        message = "Sketch rules satisfied.";
        return true;
    }

    private void TryPaintAtScreenPosition(Vector3 screenPosition)
    {
        if (sceneCamera == null || baseTilemap == null)
        {
            return;
        }

        screenPosition.z = Mathf.Abs(
            sceneCamera.transform.position.z
            - baseTilemap.transform.position.z
        );
        Vector3 worldPosition = sceneCamera.ScreenToWorldPoint(screenPosition);
        Vector3Int cellPosition = baseTilemap.WorldToCell(worldPosition);

        if (!TryGetLocalCell(cellPosition, out int localX, out int localY))
        {
            return;
        }

        if (hasLastPaintedCell && cellPosition == lastPaintedCell)
        {
            return;
        }

        lastPaintedCell = cellPosition;
        hasLastPaintedCell = true;
        PaintCell(cellPosition, localX, localY);
    }

    private bool TryGetLocalCell(
        Vector3Int cellPosition,
        out int localX,
        out int localY)
    {
        localX = cellPosition.x - bottomLeftCell.x;
        localY = cellPosition.y - bottomLeftCell.y;

        return localX >= 0
            && localX < mapSize.x
            && localY >= 0
            && localY < mapSize.y;
    }

    private void PaintCell(Vector3Int cellPosition, int localX, int localY)
    {
        char previousTile = sketchCells[localX, localY];
        char nextTile = GetSelectedTileCharacter();

        if (previousTile == nextTile)
        {
            return;
        }

        if (nextTile == LevelData.Box
            && startCount >= maximumStartCount)
        {
            return;
        }

        if (nextTile == LevelData.Target
            && targetCount >= maximumTargetCount)
        {
            return;
        }

        RemoveFromCounts(previousTile);
        AddToCounts(nextTile);
        sketchCells[localX, localY] = nextTile;
        ApplyVisualTile(cellPosition, nextTile);
        UpdateCountText();
        InvalidateCheck();
    }

    private char GetSelectedTileCharacter()
    {
        switch (selectedBrush)
        {
            case SketchBrush.Wall:
                return LevelData.Wall;
            case SketchBrush.BoxStart:
                return LevelData.Box;
            case SketchBrush.Target:
                return LevelData.Target;
            default:
                return LevelData.Empty;
        }
    }

    private void ApplyVisualTile(Vector3Int cellPosition, char tile)
    {
        if (wallTilemap != null)
        {
            wallTilemap.SetTile(
                cellPosition,
                tile == LevelData.Wall ? wallTile : null
            );
            wallTilemap.RefreshAllTiles();
        }

        if (markerTilemap == null)
        {
            return;
        }

        if (tile == LevelData.Box)
        {
            markerTilemap.SetTile(cellPosition, startTile);
        }
        else if (tile == LevelData.Target)
        {
            markerTilemap.SetTile(cellPosition, targetTile);
        }
        else
        {
            markerTilemap.SetTile(cellPosition, null);
        }
    }

    private void RemoveFromCounts(char tile)
    {
        if (tile == LevelData.Box)
        {
            startCount = Mathf.Max(0, startCount - 1);
        }
        else if (tile == LevelData.Target)
        {
            targetCount = Mathf.Max(0, targetCount - 1);
        }
    }

    private void AddToCounts(char tile)
    {
        if (tile == LevelData.Box)
        {
            startCount++;
        }
        else if (tile == LevelData.Target)
        {
            targetCount++;
        }
    }

    private void ClearEditableTilemaps()
    {
        wallTilemap?.ClearAllTiles();
        markerTilemap?.ClearAllTiles();
    }

    private bool AreMapEdgesClosed()
    {
        if (mapSize.x < 2 || mapSize.y < 2)
        {
            return false;
        }

        int rightEdge = mapSize.x - 1;
        int topEdge = mapSize.y - 1;

        for (int x = 0; x < mapSize.x; x++)
        {
            if (sketchCells[x, 0] != LevelData.Wall
                || sketchCells[x, topEdge] != LevelData.Wall)
            {
                return false;
            }
        }

        for (int y = 0; y < mapSize.y; y++)
        {
            if (sketchCells[0, y] != LevelData.Wall
                || sketchCells[rightEdge, y] != LevelData.Wall)
            {
                return false;
            }
        }

        return true;
    }

    private int CountLargestEnclosedGroundArea()
    {
        if (sketchCells == null)
        {
            return 0;
        }

        bool[,] outsideGround = FindGroundConnectedToMapEdge();
        bool[,] visitedGround = new bool[mapSize.x, mapSize.y];
        int largestArea = 0;

        for (int y = 0; y < mapSize.y; y++)
        {
            for (int x = 0; x < mapSize.x; x++)
            {
                if (!IsGroundCell(x, y)
                    || outsideGround[x, y]
                    || visitedGround[x, y])
                {
                    continue;
                }

                int area = VisitGroundRegion(
                    new Vector2Int(x, y),
                    outsideGround,
                    visitedGround
                );
                largestArea = Mathf.Max(largestArea, area);
            }
        }

        return largestArea;
    }

    private bool[,] FindGroundConnectedToMapEdge()
    {
        bool[,] outsideGround = new bool[mapSize.x, mapSize.y];
        Queue<Vector2Int> openCells = new Queue<Vector2Int>();

        for (int x = 0; x < mapSize.x; x++)
        {
            EnqueueGroundCell(new Vector2Int(x, 0), outsideGround, openCells);
            EnqueueGroundCell(
                new Vector2Int(x, mapSize.y - 1),
                outsideGround,
                openCells
            );
        }

        for (int y = 0; y < mapSize.y; y++)
        {
            EnqueueGroundCell(new Vector2Int(0, y), outsideGround, openCells);
            EnqueueGroundCell(
                new Vector2Int(mapSize.x - 1, y),
                outsideGround,
                openCells
            );
        }

        while (openCells.Count > 0)
        {
            Vector2Int current = openCells.Dequeue();

            for (int i = 0; i < cardinalDirections.Length; i++)
            {
                EnqueueGroundCell(
                    current + cardinalDirections[i],
                    outsideGround,
                    openCells
                );
            }
        }

        return outsideGround;
    }

    private void EnqueueGroundCell(
        Vector2Int position,
        bool[,] visitedGround,
        Queue<Vector2Int> openCells)
    {
        if (!IsInsideMap(position)
            || !IsGroundCell(position.x, position.y)
            || visitedGround[position.x, position.y])
        {
            return;
        }

        visitedGround[position.x, position.y] = true;
        openCells.Enqueue(position);
    }

    private int VisitGroundRegion(
        Vector2Int start,
        bool[,] blockedGround,
        bool[,] visitedGround)
    {
        Queue<Vector2Int> openCells = new Queue<Vector2Int>();
        openCells.Enqueue(start);
        visitedGround[start.x, start.y] = true;
        int area = 0;

        while (openCells.Count > 0)
        {
            Vector2Int current = openCells.Dequeue();
            area++;

            for (int i = 0; i < cardinalDirections.Length; i++)
            {
                Vector2Int next = current + cardinalDirections[i];

                if (!IsInsideMap(next)
                    || !IsGroundCell(next.x, next.y)
                    || blockedGround[next.x, next.y]
                    || visitedGround[next.x, next.y])
                {
                    continue;
                }

                visitedGround[next.x, next.y] = true;
                openCells.Enqueue(next);
            }
        }

        return area;
    }

    private bool AreAllGroundCellsSingleConnectedRegion()
    {
        Vector2Int firstGroundCell = Vector2Int.zero;
        bool foundGroundCell = false;
        int totalGroundCells = 0;

        for (int y = 0; y < mapSize.y; y++)
        {
            for (int x = 0; x < mapSize.x; x++)
            {
                if (!IsGroundCell(x, y))
                {
                    continue;
                }

                totalGroundCells++;

                if (!foundGroundCell)
                {
                    firstGroundCell = new Vector2Int(x, y);
                    foundGroundCell = true;
                }
            }
        }

        if (!foundGroundCell)
        {
            return false;
        }

        bool[,] visitedGround = new bool[mapSize.x, mapSize.y];
        bool[,] noBlockedGround = new bool[mapSize.x, mapSize.y];
        int connectedGroundCells = VisitGroundRegion(
            firstGroundCell,
            noBlockedGround,
            visitedGround
        );

        return connectedGroundCells == totalGroundCells;
    }

    private bool IsInsideMap(Vector2Int position)
    {
        return position.x >= 0
            && position.x < mapSize.x
            && position.y >= 0
            && position.y < mapSize.y;
    }

    private bool IsGroundCell(int x, int y)
    {
        return sketchCells[x, y] != LevelData.Wall;
    }

    private void UpdateCountText()
    {
        if (countText != null)
        {
            countText.text = "Starts: "
                + startCount
                + "/"
                + maximumStartCount
                + "    Targets: "
                + targetCount
                + "/"
                + maximumTargetCount
                + "    Area: "
                + CountLargestEnclosedGroundArea()
                + " (min "
                + minimumActivityArea
                + ")";
        }
    }

    private void SetStatus(string message)
    {
        if (statusText != null)
        {
            statusText.text = message;
        }
    }

    private void InvalidateCheck()
    {
        SetSubmitInteractable(false);
        ClearWarning();
    }

    private void SetSubmitInteractable(bool interactable)
    {
        if (submitButton != null)
        {
            submitButton.interactable = interactable;
        }
    }

    private void ClearWarning()
    {
        if (warningText != null)
        {
            warningText.text = "";
        }
    }

    private void ShowWarning(string message, Color color)
    {
        if (warningText != null)
        {
            warningText.color = color;
            warningText.text = message;
        }
    }

    private void ValidateSceneReferences()
    {
        if (sceneCamera == null
            || baseTilemap == null
            || wallTilemap == null
            || markerTilemap == null
            || wallTile == null
            || startTile == null
            || targetTile == null
            || wallButton == null
            || startButton == null
            || targetButton == null
            || eraserButton == null
            || countText == null
            || statusText == null
            || checkButton == null
            || submitButton == null
            || warningText == null)
        {
            Debug.LogError(
                "PCLevelSketchController: A serialized scene reference is missing.",
                this
            );
        }
    }
}
