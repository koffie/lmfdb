import json
import inventory_helpers as ih
import lmfdb_inventory as inv
import inventory_db_core as idc

class UpdateFailed(Exception):
    """Raise for failure to update"""
    def __init__(self, message):
        mess = "Failed to update field "+message
        super(Exception, self).__init__(mess)

def update_fields(diff, storeRollback=True):
    """Update a record from a diff object.

    diff -- should be a fully qualified difference, containing db, collection names and then a list of changes, each being a dict containing the item, the field and the new content. Item corresponds to an entry in an object, field to the piece of information this specifies (for example, type, description, example)
    e.g. {"db":"curve_automorphisms","collection":"passports","diffs":[{"item":"total_label","field":"type","content":"string"}]}
    storeRollback -- determine whether to store the undiff and diff to allow rollback of the change
    """

    try:
        got_client = inv.setup_internal_client(editor=True)
        assert(got_client == True)
        db = inv.int_client[inv.get_inv_db_name()]
    except Exception as e:
        inv.log_dest.error("Error getting Db connection "+ str(e))
        return

    try:
        if diff['collection'] is not None:
            inv.log_dest.info("Updating descriptions for " + diff["db"]+'.'+diff["collection"])
        else:
            inv.log_dest.info("Updating descriptions for " + diff["db"])
        _id = idc.get_db_id(db, diff["db"])
        rollback = None
        try:
            for change in diff["diffs"]:
                if ih.is_special_field(change["item"]):
                    if storeRollback:
                        rollback = capture_rollback(db, _id['id'], diff["db"], diff["collection"], change)
                    change["item"] = change["item"][2:-2] #Trim special fields. TODO this should be done better somehow
                    updated = idc.update_coll_data(db, _id['id'], diff["collection"], change["item"], change["field"], change["content"])
                elif ih.is_toplevel_field(change["item"]):
                    #Here we have item == "toplevel", field the relevant field, and change the new value
                    if storeRollback:
                        rollback = capture_rollback(db, _id['id'], diff["db"], diff["collection"], change)
                    #Only nice_name is currently an option
                    if(change["field"] not in ['nice_name']):
                        updated = {'err':True}
                    else:
                        if(diff["collection"]):
                            c_id = idc.get_coll_id(db, _id['id'], diff['collection'])
                            updated = idc.update_coll(db, c_id['id'], nice_name=change["content"])
                        else:
                            #Is database nice_name
                            updated = idc.update_db(db, _id['id'], nice_name=change["content"])
                else:
                    _c_id = idc.get_coll_id(db, _id['id'], diff["collection"])
                    if storeRollback:
                        rollback = capture_rollback(db, _id['id'], diff["db"], diff["collection"], change, coll_id = _c_id['id'])
                    updated = idc.update_field(db, _c_id['id'], change["item"], change["field"], change["content"], type="human")

                if updated['err']:
                    raise KeyError("Cannot update, item not present")
                else:
                    if storeRollback:
                        store_rollback(db, rollback)

        except Exception as e:
            inv.log_dest.error("Error applying diff "+ str(change)+' '+str(e))
            raise UpdateFailed(str(e))

    except Exception as e:
        inv.log_dest.error("Error updating fields "+ str(e))

def capture_rollback(inv_db, db_id, db_name, coll_name, change, coll_id = None):
    """"Capture diff which will allow roll-back of edits

    inv_db -- connection to inventory_db
    db_id -- ID of DB change applies to
    db_name -- Name of DB change applies to
    coll_name -- Name of collection change applies to
    change -- The change to be made, as a diff item ( entry in diff['diffs'])

    coll_id -- Supply if this is a field edit and so coll_id is known
    Roll-backs can be applied using apply_rollback. Their format is a diff, with extra 'post' field storing the state after change, and the live field which should be unset if they are applied
    """

    #Fetch the current state
    if coll_id is None and coll_name is not None:
        current_record = idc.get_coll(inv_db, db_id, coll_name)
    elif coll_id is None:
        current_record = idc.get_db(inv_db, db_name)
    else:
        current_record = idc.get_field(inv_db, coll_id, change['item'], type = 'human')

    #Create a roll-back document
    field = change["field"]
    prior = change.copy()
    if coll_id is None and coll_name is not None:
        if ih.is_special_field(change["item"]):
            prior['content'] = current_record['data'][change["item"][2:-2]][field]
        elif ih.is_toplevel_field(change['item']):
            prior['content'] = current_record['data'][field]
        else:
            prior['content'] = current_record['data'][change["item"]][field]
    elif coll_id is None:
        prior['content'] = current_record['data'][field]
    else:
        prior['content'] = current_record['data']['data'][field]

    #This can be applied like the diffs from the web-frontend, but has an extra field
    rollback_diff = {"db":db_name, "collection":coll_name, "diffs":[prior], "post":change, "live":True}

    return rollback_diff

def store_rollback(inv_db, rollback_diff):
    """"Store a rollback to allow roll-back of edits

    inv_db -- connection to inventory_db
    rollback_diff -- rollback record to store
    Roll-backs can be applied using apply_rollback. Their format is a diff, with extra 'post' field
    """
    if not rollback_diff:
        return {'err':True, 'id':0}

    #Commit to db
    try:
        table_name = inv.ALL_STRUC.rollback_human[inv.STR_NAME]
        coll = inv_db[table_name]
    except Exception as e:
        inv.log_dest.error("Error getting collection "+str(e))
        return {'err':True, 'id':0}
    fields = inv.ALL_STRUC.rollback_human[inv.STR_CONTENT]
    record = {fields[1]:rollback_diff}
    try:
        _id = coll.insert(record)
    except Exception as e:
        inv.log_dest.error("Error inserting new record" +str(e))
        return {'err':True, 'id':0}

def set_rollback_dead(inv_db, rollback_doc):
    """Set rollback to dead (live = False) e.g. after application

    inv_db -- LMFDB connection to inventory db
    rollback_doc -- Rollback entry. Got using e.g. inv_db[inv.ALL_STRUC.rollback_human[inv.STR_NAME]].find_one()

    """
    #Because we're using nexted documents, we capture the entire record, modify and return
    rollback_coll = inv_db[inv.ALL_STRUC.rollback_human[inv.STR_NAME]]
    id = rollback_doc['_id']
    diff = rollback_doc.copy()
    diff['diff']['live'] = False
    response = rollback_coll.find_and_modify(query={'_id':id}, update={"$set":diff}, upsert=False, full_response=True)


def apply_rollback(inv_db, rollback_doc):
    """Apply a rollback given as a fetch from the rollbacks table

    inv_db -- LMFDB connection to inventory db
    rollback_doc -- Rollback entry. Got using e.g. inv_db[inv.ALL_STRUC.rollback_human[inv.STR_NAME]].find_one()

    Throws -- UpdateFailed if diff application failed
    """
    try:
        assert(rollback_doc['diff']['live'])
        update_fields(rollback_doc['diff'], storeRollback=False)
        set_rollback_dead(inv_db, rollback_doc)
    except:
        raise
