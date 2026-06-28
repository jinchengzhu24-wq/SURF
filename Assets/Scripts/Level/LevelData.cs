using UnityEngine;

public class LevelData : MonoBehaviour
{
    public const char Ground = '.';
    public const char Wall = '#';
    public const char Water = '@';
    public const char Empty = ' ';
    public const char Player = 'p';
    public const char Box = 's';
    public const char Target = 't';

    public string[] rows =
    {
        "   #######    ",
        "  ##.....##   ",
        " ##..t....### ",
        " #.@@.......# ",
        " #.@@....s..# ",
        " ###..#.....# ",
        "   #...t.#..# ",
        "   #.s......# ",
        "   ###..#...# ",
        "     #..p..## ",
        "     #######  "
    };

    public static char GetMapTile(LevelData levelData, int x, int y)
    {
        return levelData != null ? GetMapTile(levelData.rows, x, y) : '\0';
    }

    public static char GetMapTile(LevelData levelData, Vector2Int position)
    {
        return GetMapTile(levelData, position.x, position.y);
    }

    public static char GetMapTile(string[] rows, Vector2Int position)
    {
        return GetMapTile(rows, position.x, position.y);
    }

    public static char GetMapTile(string[] rows, int x, int y)
    {
        if (rows == null || y < 0 || y >= rows.Length)
        {
            return '\0';
        }

        string row = rows[y] ?? "";

        if (x < 0 || x >= row.Length)
        {
            return '\0';
        }

        return row[x];
    }

    public static bool IsGround(char tile)
    {
        return tile == Ground || tile == Player || tile == Box || tile == Target;
    }

    public static bool IsGround(string[] rows, int x, int y)
    {
        return IsGround(GetMapTile(rows, x, y));
    }

    public static bool IsGround(string[] rows, Vector2Int position)
    {
        return IsGround(rows, position.x, position.y);
    }

    public static bool IsWall(string[] rows, int x, int y)
    {
        return GetMapTile(rows, x, y) == Wall;
    }

    public static bool IsWall(string[] rows, Vector2Int position)
    {
        return IsWall(rows, position.x, position.y);
    }

    public static bool IsWater(string[] rows, int x, int y)
    {
        return GetMapTile(rows, x, y) == Water;
    }

    public static bool IsWater(string[] rows, Vector2Int position)
    {
        return IsWater(rows, position.x, position.y);
    }

    public static bool HasTile(char tile)
    {
        return tile != '\0' && tile != Empty;
    }

    public static bool HasTile(string[] rows, int x, int y)
    {
        return HasTile(GetMapTile(rows, x, y));
    }

    public static bool HasTile(string[] rows, Vector2Int position)
    {
        return HasTile(rows, position.x, position.y);
    }

    public static bool HasTilesAround(string[] rows, int x, int y)
    {
        for (int yOffset = -1; yOffset <= 1; yOffset++)
        {
            for (int xOffset = -1; xOffset <= 1; xOffset++)
            {
                if (xOffset == 0 && yOffset == 0)
                {
                    continue;
                }

                if (!HasTile(rows, x + xOffset, y + yOffset))
                {
                    return false;
                }
            }
        }

        return true;
    }

    public static bool HasTilesAround(string[] rows, Vector2Int position)
    {
        return HasTilesAround(rows, position.x, position.y);
    }

    public static bool IsSurroundedWall(string[] rows, int x, int y)
    {
        return IsWall(rows, x, y) && HasTilesAround(rows, x, y) && !IsWater(rows, x, y + 1);
    }

    public static bool IsSurroundedWall(string[] rows, Vector2Int position)
    {
        return IsSurroundedWall(rows, position.x, position.y);
    }

    public static bool HasNormalCornerWallShape(string[] rows, int x, int y)
    {
        return IsNormalCornerWallShape(
            IsWall(rows, x, y - 1),
            IsWall(rows, x - 1, y),
            IsWall(rows, x + 1, y)
        );
    }

    public static bool HasRightAndRightDownWallShape(string[] rows, int x, int y)
    {
        return IsRightAndRightDownWallShape(IsWall(rows, x + 1, y), IsWall(rows, x + 1, y + 1));
    }

    public static bool HasVerticalWallShape(string[] rows, int x, int y)
    {
        return IsVerticalWallShape(IsWall(rows, x, y - 1), IsWall(rows, x, y + 1));
    }

    public static bool HasHorizontalWallShape(string[] rows, int x, int y)
    {
        return IsHorizontalWallShape(IsWall(rows, x - 1, y), IsWall(rows, x + 1, y));
    }

    public static bool IsSupportedWallShape(string[] rows, Vector2Int position)
    {
        if (!IsWall(rows, position))
        {
            return false;
        }

        return IsSupportedWallShape(
            IsSurroundedWall(rows, position),
            IsWall(rows, position + new Vector2Int(0, -1)),
            IsWall(rows, position + new Vector2Int(0, 1)),
            IsWall(rows, position + new Vector2Int(-1, 0)),
            IsWall(rows, position + new Vector2Int(1, 0)),
            IsWall(rows, position + new Vector2Int(1, 1))
        );
    }

    public static bool IsSupportedWallShape(
        bool surrounded,
        bool up,
        bool down,
        bool left,
        bool right,
        bool rightDown)
    {
        return surrounded
            || IsNormalCornerWallShape(up, left, right)
            || IsRightAndRightDownWallShape(right, rightDown)
            || IsVerticalWallShape(up, down)
            || IsHorizontalWallShape(left, right);
    }

    public static bool IsNormalCornerWallShape(bool up, bool left, bool right)
    {
        return up && (left || right);
    }

    public static bool IsRightAndRightDownWallShape(bool right, bool rightDown)
    {
        return right && rightDown;
    }

    public static bool IsVerticalWallShape(bool up, bool down)
    {
        return up || down;
    }

    public static bool IsHorizontalWallShape(bool left, bool right)
    {
        return left && right;
    }

    public static string GetTileNeighborhoodDebug(string[] rows, int x, int y)
    {
        return "["
            + GetDebugTile(rows, x - 1, y - 1)
            + GetDebugTile(rows, x, y - 1)
            + GetDebugTile(rows, x + 1, y - 1)
            + "/"
            + GetDebugTile(rows, x - 1, y)
            + GetDebugTile(rows, x, y)
            + GetDebugTile(rows, x + 1, y)
            + "/"
            + GetDebugTile(rows, x - 1, y + 1)
            + GetDebugTile(rows, x, y + 1)
            + GetDebugTile(rows, x + 1, y + 1)
            + "]";
    }

    public static char GetDebugTile(string[] rows, int x, int y)
    {
        char tile = GetMapTile(rows, x, y);
        return tile == '\0' ? '!' : tile;
    }
}
