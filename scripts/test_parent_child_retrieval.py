import json
import logging
from config.config_loader import ConfigLoader
from tools.milvus_tool import create_milvus_tools
from utils.parent_child_retrieval import create_parent_child_retrieval
from utils.log_utils import configure_logging


logger = logging.getLogger("scripts.test_parent_child_retrieval")

def main() -> None:
    configure_logging()
    cfg = ConfigLoader()
    milvus_config = cfg.get_milvus_config()
    embedder_config = cfg.get_embedding_config()
    tool = create_milvus_tools(milvus_config, embedder_config)

    collection = tool._get_collection()
    output_field = str(milvus_config.get("output_field", "content"))
    chunk_method_field = str(milvus_config.get("parent_child_chunk_method_field", "chunk_method"))
    pc_type_field = str(milvus_config.get("parent_child_type_field", "pc_type"))
    relation_field = str(milvus_config.get("parent_child_relation_field", "Column"))
    filter_field = str(milvus_config.get("filter_field", "file_name"))

    output_fields = ["id", output_field, filter_field, chunk_method_field, pc_type_field, relation_field]
    retriever = create_parent_child_retrieval(collection)

    children = collection.query(
        expr=f'{chunk_method_field} == "parent_child_split" and {pc_type_field} == "child" and {relation_field} != ""',
        limit=5,
        output_fields=output_fields,
    )
    parents = collection.query(
        expr=f'{chunk_method_field} == "parent_child_split" and {pc_type_field} == "parent" and {relation_field} != ""',
        limit=5,
        output_fields=output_fields,
    )

    logger.info(
        "config\n%s",
        json.dumps(
            {
                "output_field": output_field,
                "filter_field": filter_field,
                "chunk_method_field": chunk_method_field,
                "pc_type_field": pc_type_field,
                "relation_field": relation_field,
            },
            ensure_ascii=False,
        ),
    )

    logger.info("children (sample)")
    for i, row in enumerate(children or [], 1):
        content = str(row.get(output_field, "") or "")
        md = {
            "id": row.get("id"),
            "chunk_method": row.get(chunk_method_field, ""),
            "pc_type": row.get(pc_type_field, ""),
            "Column": row.get(relation_field, ""),
        }
        resolved_content, resolved_md = retriever.resolve_parent_content(md, content, row.get("id"))
        changed = resolved_content != content
        logger.info(
            "%s. child_id=%s Column=%s changed=%s parent_id=%s",
            i,
            row.get("id"),
            md.get("Column"),
            changed,
            resolved_md.get("_parent_id"),
        )

    logger.info("parents (sample)")
    for i, row in enumerate(parents or [], 1):
        logger.info(
            "%s. parent_id=%s Column=%s file_name=%s",
            i,
            row.get("id"),
            row.get(relation_field),
            row.get(filter_field),
        )


if __name__ == "__main__":
    main()
