using UnityEngine;

// 这里集中存放自动生成关卡使用的安全模板。
// LLM 只选择模板意图，真正的墙体、水面、目标点和可解性仍由本地生成器校验。
// 当前强结构模板：
// - goal_room：目标房模板，目标点靠同一侧聚集，障碍更偏向保护目标区入口。
// - bottleneck_corridor：瓶颈走廊模板，中部或侧边形成狭窄通道，强调绕路和通过瓶颈推箱。
// - split_route：分路线模板，两个目标点距离更远，障碍把路线拆成两条不同推进路径。
// - open_workshop：开放工坊模板，空间相对开阔，中央障碍制造绕行和站位选择。
// 辅助模板：
// - WallObstacleShapes：安全内部墙块形状，只使用当前墙体 tile 规则能表达的结构。
// - OuterShellTemplates：安全外墙轮廓模板，提供非矩形但闭合的房间边界。

public static class LevelGenerationTemplates
{
    public sealed class StructureTemplate
    {
        public readonly string archetype;
        public readonly int outerShellTemplateIndex;
        public readonly Vector2Int[] obstacleAnchors;
        public readonly int[] wallShapeIndices;
        public readonly Vector2Int[] targetAnchors;
        public readonly Vector2Int[] waterAnchors;

        public StructureTemplate(
            string archetype,
            int outerShellTemplateIndex,
            Vector2Int[] obstacleAnchors,
            int[] wallShapeIndices,
            Vector2Int[] targetAnchors,
            Vector2Int[] waterAnchors)
        {
            this.archetype = archetype;
            this.outerShellTemplateIndex = outerShellTemplateIndex;
            this.obstacleAnchors = obstacleAnchors;
            this.wallShapeIndices = wallShapeIndices;
            this.targetAnchors = targetAnchors;
            this.waterAnchors = waterAnchors;
        }
    }

    public static readonly string[][] WallObstacleShapes =
    {
        new string[]
        {
            "##",
            "#."
        },
        new string[]
        {
            "##",
            "#.",
            "#."
        },
        new string[]
        {
            "###",
            ".#."
        },
        new string[]
        {
            "##.",
            ".##"
        },
        new string[]
        {
            "#.#",
            "###"
        },
        new string[]
        {
            "##.",
            "#..",
            "##."
        },
        new string[]
        {
            "###",
            "#.."
        },
        new string[]
        {
            "##",
            ".#",
            ".#"
        },
        new string[]
        {
            "#"
        },
        new string[]
        {
            "#.",
            ".#"
        },
        new string[]
        {
            ".#",
            "#."
        }
    };

    public static readonly string[][] OuterShellTemplates =
    {
        new string[]
        {
            "  ########  ",
            " ##......## ",
            " #........# ",
            "##........##",
            "#..........#",
            "#..........#",
            "##........##",
            " #........# ",
            " ##......## ",
            "  ########  "
        },
        new string[]
        {
            " ###########",
            "##.........#",
            "#..........#",
            "#..........#",
            "#.........##",
            "#........## ",
            "##.......#  ",
            " ##......#  ",
            "  #......#  ",
            "  ########  "
        },
        new string[]
        {
            "   #######  ",
            "  ##.....## ",
            " ##.......##",
            " #.........#",
            "##.........#",
            "#..........#",
            "#.........##",
            "##.......## ",
            " ##.....##  ",
            "  #######   "
        },
        new string[]
        {
            " #########  ",
            " #.......#  ",
            "##.......#  ",
            "#........## ",
            "#.........# ",
            "##........# ",
            " #........##",
            " #.........#",
            " ##.......# ",
            "  ######### "
        },
        new string[]
        {
            "  ######### ",
            " ##.......# ",
            " #........# ",
            " #........##",
            "##.........#",
            "#..........#",
            "#.........##",
            "##........# ",
            " #.......## ",
            " #########  "
        },
        new string[]
        {
            " ########   ",
            "##......##  ",
            "#........#  ",
            "#.........# ",
            "##........##",
            " #.........#",
            " #.........#",
            " ##.......##",
            "  ##.....## ",
            "   #######  "
        },
        new string[]
        {
            "  ########  ",
            " ##......## ",
            "##........# ",
            "#.........# ",
            "#........## ",
            "##.......#  ",
            " #.......## ",
            " #........# ",
            " ##......## ",
            "  ########  "
        }
    };

    public static readonly StructureTemplate[] StructureTemplates =
    {
        new StructureTemplate(
            "goal_room",
            0,
            new Vector2Int[]
            {
                new Vector2Int(6, 4),
                new Vector2Int(6, 5),
                new Vector2Int(5, 4),
                new Vector2Int(7, 5)
            },
            new int[] { 1, 6, 7, 8, 9 },
            new Vector2Int[]
            {
                new Vector2Int(8, 4),
                new Vector2Int(8, 5),
                new Vector2Int(9, 3),
                new Vector2Int(9, 6)
            },
            new Vector2Int[]
            {
                new Vector2Int(2, 6),
                new Vector2Int(3, 6),
                new Vector2Int(2, 4),
                new Vector2Int(3, 7)
            }),
        new StructureTemplate(
            "bottleneck_corridor",
            1,
            new Vector2Int[]
            {
                new Vector2Int(4, 4),
                new Vector2Int(5, 5),
                new Vector2Int(6, 4),
                new Vector2Int(7, 5)
            },
            new int[] { 0, 1, 3, 7, 8, 10 },
            new Vector2Int[]
            {
                new Vector2Int(8, 3),
                new Vector2Int(8, 6),
                new Vector2Int(9, 4),
                new Vector2Int(4, 7)
            },
            new Vector2Int[]
            {
                new Vector2Int(2, 5),
                new Vector2Int(3, 6),
                new Vector2Int(8, 5)
            }),
        new StructureTemplate(
            "split_route",
            2,
            new Vector2Int[]
            {
                new Vector2Int(5, 4),
                new Vector2Int(6, 5),
                new Vector2Int(4, 5),
                new Vector2Int(7, 4)
            },
            new int[] { 2, 3, 4, 5, 8, 9, 10 },
            new Vector2Int[]
            {
                new Vector2Int(3, 3),
                new Vector2Int(8, 6),
                new Vector2Int(3, 6),
                new Vector2Int(8, 3)
            },
            new Vector2Int[]
            {
                new Vector2Int(5, 6),
                new Vector2Int(6, 6),
                new Vector2Int(2, 5),
                new Vector2Int(9, 5)
            }),
        new StructureTemplate(
            "open_workshop",
            0,
            new Vector2Int[]
            {
                new Vector2Int(5, 4),
                new Vector2Int(6, 4),
                new Vector2Int(4, 6),
                new Vector2Int(7, 6)
            },
            new int[] { 2, 3, 6, 8, 9 },
            new Vector2Int[]
            {
                new Vector2Int(4, 4),
                new Vector2Int(7, 5),
                new Vector2Int(3, 6),
                new Vector2Int(8, 3)
            },
            new Vector2Int[]
            {
                new Vector2Int(2, 6),
                new Vector2Int(9, 6)
            }),
        new StructureTemplate(
            "goal_room",
            3,
            new Vector2Int[]
            {
                new Vector2Int(6, 3),
                new Vector2Int(7, 4),
                new Vector2Int(5, 6),
                new Vector2Int(8, 6)
            },
            new int[] { 0, 2, 6, 8, 9, 10 },
            new Vector2Int[]
            {
                new Vector2Int(8, 3),
                new Vector2Int(9, 4),
                new Vector2Int(8, 6),
                new Vector2Int(9, 7)
            },
            new Vector2Int[]
            {
                new Vector2Int(2, 5),
                new Vector2Int(3, 6),
                new Vector2Int(4, 7)
            }),
        new StructureTemplate(
            "bottleneck_corridor",
            4,
            new Vector2Int[]
            {
                new Vector2Int(3, 4),
                new Vector2Int(4, 5),
                new Vector2Int(6, 4),
                new Vector2Int(7, 5)
            },
            new int[] { 1, 3, 5, 7, 8, 10 },
            new Vector2Int[]
            {
                new Vector2Int(8, 2),
                new Vector2Int(9, 5),
                new Vector2Int(7, 7),
                new Vector2Int(3, 7)
            },
            new Vector2Int[]
            {
                new Vector2Int(2, 4),
                new Vector2Int(3, 5),
                new Vector2Int(8, 6)
            }),
        new StructureTemplate(
            "split_route",
            5,
            new Vector2Int[]
            {
                new Vector2Int(5, 3),
                new Vector2Int(6, 4),
                new Vector2Int(5, 6),
                new Vector2Int(7, 6)
            },
            new int[] { 2, 3, 4, 5, 8, 9, 10 },
            new Vector2Int[]
            {
                new Vector2Int(2, 3),
                new Vector2Int(3, 6),
                new Vector2Int(8, 3),
                new Vector2Int(9, 6)
            },
            new Vector2Int[]
            {
                new Vector2Int(5, 5),
                new Vector2Int(6, 5),
                new Vector2Int(7, 6),
                new Vector2Int(2, 6)
            }),
        new StructureTemplate(
            "open_workshop",
            6,
            new Vector2Int[]
            {
                new Vector2Int(4, 3),
                new Vector2Int(7, 3),
                new Vector2Int(5, 5),
                new Vector2Int(8, 6)
            },
            new int[] { 0, 2, 3, 6, 8, 9 },
            new Vector2Int[]
            {
                new Vector2Int(3, 4),
                new Vector2Int(4, 6),
                new Vector2Int(7, 3),
                new Vector2Int(8, 6)
            },
            new Vector2Int[]
            {
                new Vector2Int(2, 5),
                new Vector2Int(8, 5),
                new Vector2Int(3, 7),
                new Vector2Int(9, 6)
            })
    };

    public static StructureTemplate GetStructureTemplate(string archetype)
    {
        for (int i = 0; i < StructureTemplates.Length; i++)
        {
            if (StructureTemplates[i].archetype == archetype)
            {
                return StructureTemplates[i];
            }
        }

        return StructureTemplates[0];
    }
}
