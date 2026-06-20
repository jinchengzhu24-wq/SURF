using System.Collections;
using System.Collections.Generic;
using UnityEngine;

public class LevelData : MonoBehaviour
{
    public string[] rows =
    {
        "   #######    ",
        "  ##     ##   ",
        " ##  t    ### ",
        " #   ###    # ",
        " # @@@   s  # ",
        " ###  ##    # ",
        "   #   t ## # ",
        "   # s    @ # ",
        "   ### ##   # ",
        "     #  p  ## ",
        "     #######  "
    };
    //p = 玩家起点，同时生成 Player
    //s = 箱子起点，同时生成 Start + Box
    //t = 箱子终点，同时生成 Target
    //@ = 水
    //# = 墙
    //空格 = 地面
}
