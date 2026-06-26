"""RPA文件解包器 - 解包Ren'Py的.rpa打包文件"""

import os
import re
import struct
import zlib
import pickle
from pathlib import Path
from typing import Dict, List, Optional


class RPAExtractor:
    """RPA文件解包器"""

    def __init__(self):
        self.supported_versions = ['RPA-3.0', 'RPA-2.0', 'RPA-1.0']

    def extract_rpa(self, rpa_path: str, output_dir: str = None) -> Dict[str, bytes]:
        """解包.rpa文件"""
        rpa_path = Path(rpa_path)

        if output_dir is None:
            output_dir = rpa_path.parent / rpa_path.stem
        else:
            output_dir = Path(output_dir)

        output_dir.mkdir(parents=True, exist_ok=True)

        with open(rpa_path, 'rb') as f:
            # 读取文件头
            header = f.readline().decode('utf-8').strip()
            print(f"文件头: {header}")

            # 检查版本
            version = header.split()[0]
            if version not in self.supported_versions:
                raise ValueError(f"不支持的RPA版本: {version}")

            # 解析索引偏移量
            if version == 'RPA-3.0':
                # RPA-3.0: offset和key在同一行
                parts = header.split()
                offset = int(parts[1], 16)
                key = int(parts[2], 16) if len(parts) > 2 else 0
            elif version == 'RPA-2.0':
                # RPA-2.0: 只有offset
                parts = header.split()
                offset = int(parts[1], 16)
                key = 0
            else:
                # RPA-1.0: 简单格式
                offset = int(header.split()[1], 16)
                key = 0

            print(f"索引偏移: {offset}, Key: {key}")

            # 读取索引
            f.seek(offset)
            index_data = f.read()

            print(f"原始索引数据长度: {len(index_data)}")
            print(f"原始索引数据前50字节: {index_data[:50]}")

            # RPA-3.0索引通常是zlib压缩的，先尝试解压
            try:
                decompressed = zlib.decompress(index_data)
                index_data = decompressed
                print(f"解压后数据长度: {len(index_data)}")
                print(f"解压后数据前50字节: {index_data[:50]}")
            except Exception as e:
                print(f"解压失败: {e}")
                # 如果解压失败，可能是需要先解密
                if key:
                    index_data = self._decrypt_index(index_data, key)
                    print(f"解密后数据前50字节: {index_data[:50]}")
                    try:
                        decompressed = zlib.decompress(index_data)
                        index_data = decompressed
                        print(f"解密解压后数据长度: {len(index_data)}")
                    except Exception as e2:
                        print(f"解密解压失败: {e2}")

            # 解析索引
            index = None
            try:
                index = pickle.loads(index_data)
            except Exception as e:
                print(f"标准解析失败: {e}")
                print("尝试简单解析...")
                index = self._parse_index_simple(index_data)

            if not index:
                print("无法解析索引")
                return {}

            print(f"找到 {len(index)} 个文件")

            # 提取文件
            extracted = {}
            for filename, file_info in index.items():
                try:
                    # 解码文件名
                    if isinstance(filename, bytes):
                        filename = filename.decode('utf-8')

                    print(f"处理文件: {filename}, 类型: {type(file_info)}")

                    # 获取文件数据（可能是tuple或list，或者包含tuple的list）
                    if isinstance(file_info, (tuple, list)):
                        # 如果是包含单个元组的列表，提取元组
                        if len(file_info) == 1 and isinstance(file_info[0], (tuple, list)):
                            file_info = file_info[0]

                        # RPA-3.0格式: (offset, length, prefix)
                        if len(file_info) >= 2:
                            file_offset = file_info[0]
                            file_length = file_info[1]
                            prefix = file_info[2] if len(file_info) > 2 else b''
                            if isinstance(prefix, int):
                                prefix = b''

                            # RPA-3.0使用XOR密钥加密偏移量
                            if key:
                                file_offset = file_offset ^ key
                                file_length = file_length ^ key

                            print(f"  偏移: {file_offset}, 长度: {file_length}")
                        else:
                            continue
                    else:
                        continue

                    # 读取文件数据
                    f.seek(file_offset)
                    file_data = f.read(file_length)

                    # 添加前缀（如果有）
                    if prefix:
                        file_data = prefix + file_data

                    print(f"  数据长度: {len(file_data)}")

                    # 保存文件
                    output_path = output_dir / filename
                    output_path.parent.mkdir(parents=True, exist_ok=True)

                    with open(output_path, 'wb') as out_f:
                        out_f.write(file_data)

                    extracted[filename] = file_data
                    print(f"  提取成功: {output_path}")

                except Exception as e:
                    print(f"  提取失败: {e}")
                    import traceback
                    traceback.print_exc()
                    continue

            return extracted

    def _decrypt_index(self, data: bytes, key: int) -> bytes:
        """解密索引数据"""
        # RPA-3.0使用简单的XOR加密
        key_bytes = struct.pack('<I', key)
        decrypted = bytearray(len(data))
        for i in range(len(data)):
            decrypted[i] = data[i] ^ key_bytes[i % 4]
        return bytes(decrypted)

    def _parse_index_simple(self, data: bytes) -> dict:
        """简单解析索引数据（当pickle失败时使用）"""
        # 尝试从二进制数据中提取文件名和偏移量
        index = {}
        try:
            # 查找文件名模式
            pos = 0
            while pos < len(data):
                # 查找.rpy或.rpyc文件名
                match = re.search(rb'([a-zA-Z0-9_/\\]+\.rpyc?)', data[pos:])
                if not match:
                    break

                filename = match.group(1).decode('utf-8', errors='ignore')
                pos += match.end()

                # 查找后续的数字（偏移量和长度）
                num_match = re.search(rb'(\d{5,})', data[pos:])
                if num_match:
                    try:
                        offset = int(num_match.group(1))
                        # 尝试找到长度
                        len_match = re.search(rb'(\d{3,})', data[pos + num_match.end():])
                        if len_match:
                            length = int(len_match.group(1))
                            index[filename] = (offset, length, b'')
                    except Exception as parse_err:
                        print(f"解析索引行失败: {parse_err}")

        except Exception as e:
            print(f"简单解析失败: {e}")

        return index

    def _decompress_index(self, data: bytes) -> bytes:
        """解压索引数据"""
        try:
            # 尝试zlib解压
            if data[:2] == b'x\x9c':
                return zlib.decompress(data)
            # 尝试带头部的zlib解压
            elif data[:4] == b'\x78\x9c':
                return zlib.decompress(data)
            # 尝试raw deflate
            else:
                return zlib.decompress(data, -15)
        except Exception as e:
            print(f"解压失败: {e}")
            return data

    def list_files(self, rpa_path: str) -> List[str]:
        """列出.rpa文件中的所有文件"""
        rpa_path = Path(rpa_path)

        with open(rpa_path, 'rb') as f:
            # 读取文件头
            header = f.readline().decode('utf-8').strip()

            # 检查版本
            version = header.split()[0]
            if version not in self.supported_versions:
                raise ValueError(f"不支持的RPA版本: {version}")

            # 解析索引偏移量
            if version == 'RPA-3.0':
                parts = header.split()
                offset = int(parts[1], 16)
                key = int(parts[2], 16) if len(parts) > 2 else 0
            elif version == 'RPA-2.0':
                parts = header.split()
                offset = int(parts[1], 16)
                key = 0
            else:
                offset = int(header.split()[1], 16)
                key = 0

            # 读取索引
            f.seek(offset)
            index_data = f.read()

            # RPA-3.0索引通常是zlib压缩的，先尝试解压
            try:
                decompressed = zlib.decompress(index_data)
                index_data = decompressed
            except Exception as decomp_err:
                print(f"索引解压失败，尝试解密: {decomp_err}")
                # 如果解压失败，可能是需要先解密
                if key:
                    index_data = self._decrypt_index(index_data, key)
                    try:
                        decompressed = zlib.decompress(index_data)
                        index_data = decompressed
                    except Exception as decomp_err2:
                        print(f"解密后解压仍然失败: {decomp_err2}")

            # 解析索引
            index = None
            try:
                index = pickle.loads(index_data)
                # 打印第一个文件的信息用于调试
                if index:
                    first_fn, first_info = list(index.items())[0]
                    print(f"第一个文件: {first_fn}")
                    print(f"文件信息: {first_info}")
            except Exception as e:
                print(f"解析索引失败: {e}")
                return []

            # 返回文件列表
            files = []
            for filename in index.keys():
                if isinstance(filename, bytes):
                    filename = filename.decode('utf-8')
                files.append(filename)

            return files


def extract_game_scripts(game_dir: str, output_dir: str = None) -> str:
    """提取游戏目录中的所有脚本文件"""
    game_path = Path(game_dir)

    # 查找.rpa文件
    rpa_files = list(game_path.glob('*.rpa')) + list((game_path / 'game').glob('*.rpa'))

    if not rpa_files:
        print("未找到.rpa文件")
        return ""

    if output_dir is None:
        output_dir = str(game_path / 'extracted_scripts')

    extractor = RPAExtractor()
    all_extracted = {}

    for rpa_file in rpa_files:
        print(f"\n处理: {rpa_file.name}")
        try:
            extracted = extractor.extract_rpa(str(rpa_file), output_dir)
            all_extracted.update(extracted)
        except Exception as e:
            print(f"处理 {rpa_file.name} 失败: {e}")

    print(f"\n总共提取了 {len(all_extracted)} 个文件")
    return output_dir


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("用法: python rpa_extractor.py <游戏目录>")
        sys.exit(1)

    game_dir = sys.argv[1]
    extract_game_scripts(game_dir)
