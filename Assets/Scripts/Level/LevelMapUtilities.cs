public static class LevelMapUtilities
{
    private const int StandardHeight = 10;
    private const int StandardWidth = 12;
    private const string PlayableAllowedTiles = " #.@pst";

    public static string[] CloneRows(string[] rows)
    {
        if (rows == null)
        {
            return null;
        }

        string[] clone = new string[rows.Length];

        for (int row = 0; row < rows.Length; row++)
        {
            clone[row] = rows[row] ?? "";
        }

        return clone;
    }

    public static bool TryValidatePlayableRows(
        string[] rows,
        string subjectLabel,
        out string error)
    {
        string subject = string.IsNullOrEmpty(subjectLabel) ? "level" : subjectLabel;
        error = "";

        if (rows == null || rows.Length != StandardHeight)
        {
            error = "The " + subject + " must contain exactly 10 rows.";
            return false;
        }

        int playerCount = 0;
        int boxCount = 0;
        int targetCount = 0;

        for (int row = 0; row < rows.Length; row++)
        {
            if (rows[row] == null || rows[row].Length != StandardWidth)
            {
                error = "Every " + subject + " row must contain exactly 12 tiles.";
                return false;
            }

            for (int column = 0; column < rows[row].Length; column++)
            {
                char tile = rows[row][column];

                if (PlayableAllowedTiles.IndexOf(tile) < 0)
                {
                    error = "The " + subject + " contains an unsupported tile.";
                    return false;
                }

                if (tile == LevelData.Player)
                {
                    playerCount++;
                }
                else if (tile == LevelData.Box)
                {
                    boxCount++;
                }
                else if (tile == LevelData.Target)
                {
                    targetCount++;
                }
            }
        }

        if (playerCount != 1
            || boxCount < 1
            || boxCount > 2
            || targetCount != boxCount)
        {
            error = "The " + subject + " has invalid player or box data.";
            return false;
        }

        return true;
    }

    public static string ComputeMapHash(string[] rows)
    {
        if (rows == null)
        {
            return "";
        }

        unchecked
        {
            uint hash = 2166136261;

            for (int i = 0; i < rows.Length; i++)
            {
                string row = rows[i] ?? "";

                for (int j = 0; j < row.Length; j++)
                {
                    hash ^= row[j];
                    hash *= 16777619;
                }

                hash ^= '\n';
                hash *= 16777619;
            }

            return hash.ToString("x8");
        }
    }
}
