#!/usr/bin/env bash
# 只操作同目录独立Compose；恢复永远新建库，不覆盖已有库、不切换应用。
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

if [[ $# != 3 || ! $3 =~ ^ds_restore_[a-z0-9_]{1,40}$ ]]; then
    printf '%s\n' '用法：bash database-transfer.sh backup|restore /绝对路径/文件.dump ds_restore_唯一名称' >&2
    exit 2
fi
operation=$1
snapshot=$2
database=$3
if [[ $snapshot != /* ]]; then
    printf '%s\n' '备份文件必须使用绝对路径。' >&2
    exit 2
fi
compose=(docker compose --env-file .env -f compose.yml)

case "$operation" in
    backup)
        umask 077
        # noclobber同时保护成功文件和失败残留；失败文件不可当作备份。
        set -C
        partial="${snapshot}.partial"
        if [[ -e $snapshot || -e $partial || -L $snapshot || -L $partial ]]; then
            printf '%s\n' '目标已存在，拒绝覆盖。' >&2
            exit 2
        fi
        "${compose[@]}" exec -T db sh -c 'exec pg_dump -U "$POSTGRES_USER" -d "$1" -Fc --no-owner --no-privileges' sh "$database" > "$partial"
        # hard link原子拒绝已存在的最终文件；输出目录需为同一普通文件系统。
        ln -- "$partial" "$snapshot"
        rm -- "$partial"
        printf '备份成功：%s\n' "$snapshot"
        ;;
    restore)
        if [[ ! -f $snapshot || ! -r $snapshot ]]; then
            printf '%s\n' '备份文件不存在或不可读。' >&2
            exit 2
        fi
        # 先检查归档可识别；不能证明数据完整，恢复后仍须对账。
        "${compose[@]}" exec -T db pg_restore --list < "$snapshot" > /dev/null
        "${compose[@]}" exec -T db sh -c 'exec createdb -U "$POSTGRES_USER" "$1"' sh "$database"
        "${compose[@]}" exec -T db sh -c 'exec pg_restore -U "$POSTGRES_USER" -d "$1" --single-transaction --exit-on-error --no-owner --no-privileges' sh "$database" < "$snapshot"
        printf '恢复成功：%s；未切换应用，未启动同步。\n' "$database"
        ;;
    *)
        printf '%s\n' '操作只允许backup或restore。' >&2
        exit 2
        ;;
esac
