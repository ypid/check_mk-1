#!/usr/bin/python
# -*- encoding: utf-8; py-indent-offset: 4 -*-
# +------------------------------------------------------------------+
# |             ____ _               _        __  __ _  __           |
# |            / ___| |__   ___  ___| | __   |  \/  | |/ /           |
# |           | |   | '_ \ / _ \/ __| |/ /   | |\/| | ' /            |
# |           | |___| | | |  __/ (__|   <    | |  | | . \            |
# |            \____|_| |_|\___|\___|_|\_\___|_|  |_|_|\_\           |
# |                                                                  |
# | Copyright Mathias Kettner 2014             mk@mathias-kettner.de |
# +------------------------------------------------------------------+
#
# This file is part of Check_MK.
# The official homepage is at http://mathias-kettner.de/check_mk.
#
# check_mk is free software;  you can redistribute it and/or modify it
# under the  terms of the  GNU General Public License  as published by
# the Free Software Foundation in version 2.  check_mk is  distributed
# in the hope that it will be useful, but WITHOUT ANY WARRANTY;  with-
# out even the implied warranty of  MERCHANTABILITY  or  FITNESS FOR A
# PARTICULAR PURPOSE. See the  GNU General Public License for more de-
# ails.  You should have  received  a copy of the  GNU  General Public
# License along with GNU Make; see the file  COPYING.  If  not,  write
# to the Free Software Foundation, Inc., 51 Franklin St,  Fifth Floor,
# Boston, MA 02110-1301 USA.

#   .--cmk -I--------------------------------------------------------------.
#   |                                  _           ___                     |
#   |                    ___ _ __ ___ | | __      |_ _|                    |
#   |                   / __| '_ ` _ \| |/ /  _____| |                     |
#   |                  | (__| | | | | |   <  |_____| |                     |
#   |                   \___|_| |_| |_|_|\_\      |___|                    |
#   |                                                                      |
#   +----------------------------------------------------------------------+
#   |  Functions for command line options -I and -II                       |
#   '----------------------------------------------------------------------'

# Function implementing cmk -I and cmk -II. This is directly
# being called from the main option parsing code. The list of
# hostnames is already prepared by the main code. If it is
# empty then we use all hosts and switch to using cache files.
def do_discovery(hostnames, check_types, only_new):
    use_caches = False
    if not hostnames:
        verbose("Discovering services on all hosts:\n")
        hostnames = all_active_realhosts()
        use_caches = True
    else:
        verbose("Discovering services on %s:\n" % ", ".join(hostnames))

    # For clusters add their nodes to the list. Clusters itself
    # cannot be discovered but the user is allowed to specify
    # them and we do discovery on the nodes instead.
    nodes = []
    for h in hostnames:
        nodes = nodes_of(h)
        if nodes:
            hostnames += nodes

    # Then remove clusters and make list unique
    hostnames = list(set([ h for h in hostnames if not is_cluster(h) ]))
    hostnames.sort()

    # Now loop through all hosts
    for hostname in hostnames:
        try:
            verbose(tty_bold + hostname + tty_normal + ":\n")
            if opt_debug:
                on_error = "raise"
            else:
                on_error = "warn"
            do_discovery_for(hostname, check_types, only_new, use_caches, on_error)
            verbose("\n")
        except Exception, e:
            if opt_debug:
                raise
            verbose(" -> Failed: %s\n" % e)

        cleanup_globals()


def do_discovery_for(hostname, check_types, only_new, use_caches, on_error):
    # Usually we disable SNMP scan if cmk -I is used without a list of
    # explicity hosts. But for host that have never been service-discovered
    # yet (do not have autochecks), we enable SNMP scan.
    do_snmp_scan = not use_caches or not has_autochecks(hostname)
    new_items = discover_services(hostname, check_types, use_caches, do_snmp_scan, on_error)
    if not check_types and not only_new:
        old_items = [] # do not even read old file
    else:
        old_items = parse_autochecks_file(hostname)

    # There are three ways of how to merge existing and new discovered checks:
    # 1. -II without --checks=
    #        check_types is empty, only_new is False
    #    --> complete drop old services, only use new ones
    # 2. -II with --checks=
    #    --> drop old services of that types
    #        check_types is not empty, only_new is False
    # 3. -I
    #    --> just add new services
    #        only_new is True

    # Parse old items into a dict (ct, item) -> paramstring
    result = {}
    for check_type, item, paramstring in old_items:
        # Take over old items if -I is selected or if -II
        # is selected with --checks= and the check type is not
        # one of the listed ones
        if only_new or (check_types and check_type not in check_types):
            result[(check_type, item)] = paramstring

    stats = {}
    for check_type, item, paramstring in new_items:
        if (check_type, item) not in result:
            result[(check_type, item)] = paramstring
            stats.setdefault(check_type, 0)
            stats[check_type] += 1

    final_items = []
    for (check_type, item), paramstring in result.items():
        final_items.append((check_type, item, paramstring))
    final_items.sort()
    save_autochecks_file(hostname, final_items)

    found_check_types = stats.keys()
    found_check_types.sort()
    if found_check_types:
        for check_type in found_check_types:
            verbose("  %s%3d%s %s\n" % (tty_green + tty_bold, stats[check_type], tty_normal, check_type))
    else:
        verbose("  nothing%s\n" % (only_new and " new" or ""))


# determine changed services on host.
# param mode: can be one of "new", "remove", "fixall", "refresh"
# param do_snmp_scan: if True, a snmp host will be scanned, otherwise uses only the check types
#                     previously discovered
def discover_on_host(mode, hostname, do_snmp_scan, use_caches, on_error="ignore"):
    counts = {
        "added"   : 0,
        "removed" : 0,
        "kept"    : 0
    }

    if hostname not in all_active_realhosts():
        return [0, 0, 0, 0], ""

    err = None

    try:
        # in "refresh" mode we first need to remove all previously discovered
        # checks of the host, so that get_host_services() does show us the
        # new discovered check parameters.
        if mode == "refresh":
            counts["removed"] += remove_autochecks_of(hostname) # this is cluster-aware!

        # Compute current state of new and existing checks
        services = get_host_services(hostname, use_caches=use_caches,
                                        do_snmp_scan=do_snmp_scan, on_error=on_error)

        # Create new list of checks
        new_items = {}
        for (check_type, item), (check_source, paramstring) in services.items():
            if check_source in ("custom", "legacy", "active", "manual"):
                continue # this is not an autocheck or ignored and currently not checked
                # Note discovered checks that are shadowed by manual checks will vanish
                # that way.

            if check_source in ("new"):
                if mode in ("new", "fixall", "refresh"):
                    counts["added"] += 1
                    new_items[(check_type, item)] = paramstring

            elif check_source in ("old", "ignored"):
                # keep currently existing valid services in any case
                new_items[(check_type, item)] = paramstring
                counts["kept"]  += 1

            elif check_source in ("obsolete", "vanished"):
                # keep item, if we are currently only looking for new services
                # otherwise fix it: remove ignored and non-longer existing services
                if mode not in ("fixall", "remove"):
                    new_items[(check_type, item)] = paramstring
                    counts["kept"] += 1
                else:
                    counts["removed"] += 1

            # Silently keep clustered services
            elif check_source.startswith("clustered_"):
                new_items[(check_type, item)] = paramstring

            else:
                raise MKGeneralException("Unknown check source '%s'" % check_source)
        set_autochecks_of(hostname, new_items)

    except Exception, e:
        if opt_debug:
            raise
        err = str(e)
    return [counts["added"], counts["removed"], counts["kept"], counts["added"] + counts["kept"]], err


#.
#   .--Discovery Check-----------------------------------------------------.
#   |           ____  _                   _               _                |
#   |          |  _ \(_)___  ___      ___| |__   ___  ___| | __            |
#   |          | | | | / __|/ __|    / __| '_ \ / _ \/ __| |/ /            |
#   |          | |_| | \__ \ (__ _  | (__| | | |  __/ (__|   <             |
#   |          |____/|_|___/\___(_)  \___|_| |_|\___|\___|_|\_\            |
#   |                                                                      |
#   +----------------------------------------------------------------------+
#   |  Active check for checking undiscovered services.                    |
#   '----------------------------------------------------------------------'

# Compute the parameters for the discovery check for a host. Note:
# if the discovery check is disabled for that host, default parameters
# will be returned.
def discovery_check_parameters(hostname):
    entries = host_extra_conf(hostname, periodic_discovery)
    if entries:
        return entries[0]
    # Support legacy global configurations
    elif inventory_check_interval:
        return default_discovery_check_parameters()
    else:
        return None


def default_discovery_check_parameters():
    return {
        "check_interval"          : inventory_check_interval,
        "severity_unmonitored"    : inventory_check_severity,
        "severity_vanished"       : 0,
        "inventory_check_do_scan" : inventory_check_do_scan,
    }



def check_discovery(hostname, ipaddress=None):
    params = discovery_check_parameters(hostname) or \
             default_discovery_check_parameters()

    try:
        # scan services, register changes
        try:
            services = get_host_services(hostname, use_caches=opt_use_cachefile,
                                        do_snmp_scan=params["inventory_check_do_scan"],
                                        on_error="raise",
                                        ipaddress=ipaddress)
        except socket.gaierror, e:
            if e[0] == -2 and not opt_debug:
                # Don't crash on unknown host name, it may be provided by the user
                raise MKAgentError(e[1])
            raise

        # generate status and infotext
        status = 0
        infotexts = []
        long_infotexts = []
        need_rediscovery = False

        for check_state, title, params_key, default_state in [
               ( "new",      "unmonitored", "severity_unmonitored", inventory_check_severity ),
               ( "vanished", "vanished",    "severity_vanished",   0 ),
            ]:

            affected_check_types = {}
            count = 0

            for (check_type, item), (check_source, paramstring) in services.items():
                if check_source == check_state:
                    count += 1
                    affected_check_types.setdefault(check_type, 0)
                    affected_check_types[check_type] += 1
                    long_infotexts.append("%s: %s: %s" % (title, check_type, service_description(check_type, item)))

            if affected_check_types:
                info = ", ".join([ "%s:%d" % e for e in affected_check_types.items() ])
                st = params.get(params_key, default_state)
                status = worst_monitoring_state(status, st)
                infotexts.append("%d %s services (%s)%s" % (count, title, info, state_markers[st]))

                if params.get("inventory_rediscovery", False):
                    mode = params["inventory_rediscovery"]["mode"]
                    if (check_state == "new"      and mode in ( 0, 2, 3 )) or \
                        check_state == "vanished" and mode in ( 1, 2, 3 ):
                        need_rediscovery = True
            else:
                infotexts.append("no %s services found" % title)

        set_rediscovery_flag(hostname, need_rediscovery)
        if need_rediscovery:
            infotexts.append("rediscovery scheduled")

        output = ", ".join(infotexts)
        if long_infotexts:
            output += "\n" + "\n".join(long_infotexts)
        output += "\n"

    except (MKSNMPError, MKAgentError), e:
        output = "Discovery failed: %s\n" % e
        # Honor rule settings for "Status of the Check_MK service". In case of
        # a problem we assume a connection error here.
        spec = exit_code_spec(hostname)
        status = spec.get("connection", 1)

    except MKCheckTimeout:
        if opt_keepalive:
            raise
        else:
            output = "Discovery failed: Timed out\n"
            spec = exit_code_spec(hostname)
            status = spec.get("timeout", 2)

    except SystemExit:
        raise

    except Exception, e:
        output = create_crash_dump(hostname, "discovery", None, None, "Check_MK Discovery", [])\
            .replace("Crash dump:\n", "Crash dump:\\n")
        if opt_debug:
            raise
        # Honor rule settings for "Status of the Check_MK service". In case of
        # a problem we assume a connection error here.
        spec = exit_code_spec(hostname)
        if isinstance(e, MKAgentError) or isinstance(e, MKSNMPError):
            what = "connection"
        else:
            what = "exception"
        status = spec.get(what, 3)

    if opt_keepalive:
        add_keepalive_result_line(output)
        return status
    else:
        sys.stdout.write(core_state_names[status] + " - " + output)
        sys.exit(status)


def set_rediscovery_flag(hostname, need_rediscovery):
    def touch(filename):
        if not os.path.exists(filename):
            f = open(filename, "w")
            f.close()

    autodiscovery_dir = var_dir + '/autodiscovery'
    discovery_filename = os.path.join(autodiscovery_dir, hostname)
    if need_rediscovery:
        if not os.path.exists(autodiscovery_dir):
            os.makedirs(autodiscovery_dir)
        touch(discovery_filename)
    else:
        if os.path.exists(discovery_filename):
            os.remove(discovery_filename)



# Run the discovery queued by check_discovery() - if any
marked_host_discovery_timeout = 120

def discover_marked_hosts():
    verbose("Doing discovery for all marked hosts:\n")

    def queue_age():
        oldest = time.time()
        for filename in os.listdir(autodiscovery_dir):
            oldest = min(oldest, os.path.getmtime(autodiscovery_dir + "/" + filename))
        return oldest

    def may_rediscover(params):
        if "inventory_rediscovery" not in params:
            return "automatic discovery disabled for this host"

        now = datetime.datetime.utcfromtimestamp(now_ts)
        for start_hours_mins, end_hours_mins in params["inventory_rediscovery"]["excluded_time"]:
            start_time = datetime.datetime(now.year, now.month, now.day, start_hours_mins[0], start_hours_mins[1])
            end_time = datetime.datetime(now.year, now.month, now.day, end_hours_mins[0], end_hours_mins[1])
            if start_time <= now <= end_time:
                return "we are currently in a disallowed time of day"

        if now_ts - oldest_queued < params["inventory_rediscovery"]["group_time"]:
            return "last activation is too recent"

        return None

    autodiscovery_dir = var_dir + '/autodiscovery'

    if not os.path.exists(autodiscovery_dir):
        # there is obviously nothing to do
        verbose("  Nothing to do. %s is missing.\n" % autodiscovery_dir)
        return

    now_ts = time.time()
    end_time_ts = now_ts + marked_host_discovery_timeout  # don't run for more than 2 minutes
    oldest_queued = queue_age()

    mode_table = {
        0: "new",
        1: "remove",
        2: "fixall",
        3: "refresh"
    }

    hosts = os.listdir(autodiscovery_dir)
    if not hosts:
        verbose("  Nothing to do. No hosts marked by discovery check.\n")
        return

    activation_required = False

    for hostname in hosts:
        verbose("%s%s%s:\n" % (tty_bold, hostname, tty_normal))
        host_flag_path = autodiscovery_dir + "/" + hostname

        if hostname not in all_configured_hosts():
            os.remove(host_flag_path)
            verbose("  Skipped. Host does not exist in configuration. Removing mark.\n")
            continue

        if time.time() > end_time_ts:
            warning("  Timeout of %d seconds reached. Lets do the remaining hosts next time." % marked_host_discovery_timeout)
            break

        # have to do hosts one-by-one because each could have a different configuration
        params = discovery_check_parameters(hostname) or default_discovery_check_parameters()
        why_not = may_rediscover(params)
        if not why_not:
            redisc_params = params["inventory_rediscovery"]
            verbose("  Doing discovery with mode '%s'...\n" % mode_table[redisc_params["mode"]])
            result, error = discover_on_host(mode_table[redisc_params["mode"]], hostname,
                                             params["inventory_check_do_scan"], True)
            if error is not None:
                if error:
                    verbose("failed: %s\n" % error)
                else:
                    # for offline hosts the error message is empty. This is to remain
                    # compatible with the automation code
                    verbose("  failed: host is offline\n")
            else:
                new_services, removed_services, kept_services, total_services = result
                if new_services == 0 and removed_services == 0 and kept_services == total_services:
                    verbose("  nothing changed.\n")
                else:
                    verbose("  %d new, %d removed, %d kept, %d total services.\n" % (tuple(result)))
                    if redisc_params["activation"]:
                        activation_required = True

            # delete the file even in error case, otherwise we might be causing the same error
            # every time the cron job runs
            os.remove(host_flag_path)
        else:
            verbose("  skipped: %s\n" % why_not)

    if activation_required:
        verbose("\nRestarting monitoring core with updated configuration...\n")
        if monitoring_core == "cmc":
            do_reload()
        else:
            do_restart()


#.
#   .--Helpers-------------------------------------------------------------.
#   |                  _   _      _                                        |
#   |                 | | | | ___| |_ __   ___ _ __ ___                    |
#   |                 | |_| |/ _ \ | '_ \ / _ \ '__/ __|                   |
#   |                 |  _  |  __/ | |_) |  __/ |  \__ \                   |
#   |                 |_| |_|\___|_| .__/ \___|_|  |___/                   |
#   |                              |_|                                     |
#   +----------------------------------------------------------------------+
#   |  Various helper functions                                            |
#   '----------------------------------------------------------------------'

def checktype_ignored_for_host(host, checktype):
    if checktype in ignored_checktypes:
        return True
    ignored = host_extra_conf(host, ignored_checks)
    for e in ignored:
        if checktype == e or (type(e) == list and checktype in e):
            return True
    return False


def service_ignored(hostname, check_type, service_description):
    if check_type and check_type in ignored_checktypes:
        return True
    if service_description != None and in_boolean_serviceconf_list(hostname, service_description, ignored_services):
        return True
    if check_type and checktype_ignored_for_host(hostname, check_type):
        return True
    return False


def get_info_for_discovery(hostname, ipaddress, section_name, use_caches):
    def add_nodeinfo(info, s):
        if s in check_info and check_info[s]["node_info"]:
            return [ [ None ] + l for l in info ]
        else:
            return info

    max_cachefile_age = use_caches and inventory_max_cachefile_age or 0
    rh_info = get_realhost_info(hostname, ipaddress, section_name, max_cachefile_age,
                                ignore_check_interval=True, use_snmpwalk_cache=False)
    if rh_info != None:
        info = apply_parse_function(add_nodeinfo(rh_info, section_name), section_name)
    else:
        info = None
    if info != None and section_name in check_info and check_info[section_name]["extra_sections"]:
        info = [ info ]
        for es in check_info[section_name]["extra_sections"]:
            try:
                bare_info = get_realhost_info(hostname, ipaddress, es, max_cachefile_age,
                                              ignore_check_interval=True, use_snmpwalk_cache=False)
                with_node_info = add_nodeinfo(bare_info, es)
                parsed = apply_parse_function(with_node_info, es)
                info.append(parsed)

            except MKAgentError:
                info.append(None)

            except:
                if opt_debug:
                    raise
                info.append(None)

    return info

#.
#   .--Discovery-----------------------------------------------------------.
#   |              ____  _                                                 |
#   |             |  _ \(_)___  ___ _____   _____ _ __ _   _               |
#   |             | | | | / __|/ __/ _ \ \ / / _ \ '__| | | |              |
#   |             | |_| | \__ \ (_| (_) \ V /  __/ |  | |_| |              |
#   |             |____/|_|___/\___\___/ \_/ \___|_|   \__, |              |
#   |                                                  |___/               |
#   +----------------------------------------------------------------------+
#   |  Core code of actual service discovery                               |
#   '----------------------------------------------------------------------'



# Create a table of autodiscovered services of a host. Do not save
# this table anywhere. Do not read any previously discovered
# services. The table has the following columns:
# 1. Check type
# 2. Item
# 3. Parameter string (not evaluated)
# Arguments:
#   check_types: None -> try all check types, list -> omit scan in any case
#   use_caches: True is cached agent data is being used (for -I without hostnames)
#   do_snmp_scan: True if SNMP scan should be done (WATO: Full scan)
# Error situation (unclear what to do):
# - IP address cannot be looked up
#
# This function does not handle:
# - clusters
# - disabled services
#
# This function *does* handle:
# - disabled check typess
#
# on_error is one of:
# "ignore" -> silently ignore any exception
# "warn"   -> output a warning on stderr
# "raise"  -> let the exception come through
def discover_services(hostname, check_types, use_caches, do_snmp_scan, on_error, ipaddress=None):
    if ipaddress == None:
        ipaddress = lookup_ip_address(hostname)

    # Check types not specified (via --checks=)? Determine automatically
    if not check_types:
        check_types = []
        if is_snmp_host(hostname):

            # May we do an SNMP scan?
            if do_snmp_scan:
                try:
                    check_types = snmp_scan(hostname, ipaddress, on_error)
                except Exception, e:
                    if on_error == "raise":
                        raise
                    elif on_error == "warn":
                        sys.stderr.write("SNMP scan failed: %s" % e)

            # Otherwise use all check types that we already have discovered
            # previously
            else:
                for check_type, item, params in read_autochecks_of(hostname):
                    if check_type not in check_types and check_uses_snmp(check_type):
                        check_types.append(check_type)

        if is_tcp_host(hostname) or has_piggyback_info(hostname):
            check_types += discoverable_check_types('tcp')

    # Make hostname available as global variable in discovery functions
    # (used e.g. by ps-discovery)
    global g_hostname
    g_hostname = hostname

    discovered_services = []
    try:
        for check_type in check_types:
            try:
                for item, paramstring in discover_check_type(hostname, ipaddress, check_type, use_caches, on_error):
                    discovered_services.append((check_type, item, paramstring))
            except (KeyboardInterrupt, MKAgentError, MKSNMPError, MKCheckTimeout):
                raise
            except Exception, e:
                if opt_debug:
                    raise
                raise MKGeneralException("Exception in check plugin '%s': %s" % (check_type, e))

        return discovered_services
    except KeyboardInterrupt:
        raise MKGeneralException("Interrupted by Ctrl-C.")


def snmp_scan(hostname, ipaddress, on_error = "ignore", for_inv=False):
    # Make hostname globally available for scan functions.
    # This is rarely used, but e.g. the scan for if/if64 needs
    # this to evaluate if_disabled_if64_checks.
    global g_hostname
    g_hostname = hostname

    vverbose("  SNMP scan:\n")
    if not in_binary_hostlist(hostname, snmp_without_sys_descr):
        sys_descr_oid = ".1.3.6.1.2.1.1.1.0"
        sys_descr = get_single_oid(hostname, ipaddress, sys_descr_oid)
        if sys_descr == None:
            raise MKSNMPError("Cannot fetch system description OID %s" % sys_descr_oid)
    else:
        # Fake OID values to prevent issues with a lot of scan functions
        set_oid_cache(hostname, ".1.3.6.1.2.1.1.1.0", "")
        set_oid_cache(hostname, ".1.3.6.1.2.1.1.2.0", "")


    found = []
    if for_inv:
        items = inv_info.items()
    else:
        items = check_info.items()

    positive_found = []
    default_found = []

    for check_type, check in items:
        if check_type in ignored_checktypes:
            continue
        elif not check_uses_snmp(check_type):
            continue
        basename = check_type.split(".")[0]
        # The scan function should be assigned to the basename, because
        # subchecks sharing the same SNMP info of course should have
        # an identical scan function. But some checks do not do this
        # correctly
        if check_type in snmp_scan_functions:
            scan_function = snmp_scan_functions[check_type]
        elif basename in snmp_scan_functions:
            scan_function = snmp_scan_functions[basename]
        elif basename in inv_info:
            scan_function = inv_info[basename].get("snmp_scan_function")
        else:
            scan_function = None

        if scan_function:
            try:
                def oid_function(oid, default_value=None):
                    value = get_single_oid(hostname, ipaddress, oid)
                    if value == None:
                        return default_value
                    else:
                        return value
                result = scan_function(oid_function)
                if result is not None and type(result) not in [ str, bool ]:
                    if on_error == "warn":
                        warning("   SNMP scan function of %s returns invalid type %s." %
                                (check_type, type(result)))
                    elif on_error == "raise":
                        raise MKGeneralException("SNMP Scan aborted.")
                elif result:
                    found.append(check_type)
                    positive_found.append(check_type)
            except MKGeneralException:
                # some error messages which we explicitly want to show to the user
                # should be raised through this
                raise
            except:
                if on_error == "warn":
                    warning("   Exception in SNMP scan function of %s" % check_type)
                elif on_error == "raise":
                    raise
                pass
        else:
            found.append(check_type)
            default_found.append(check_type)

    vverbose("   SNMP scan found:       %s%s%s%s\n" % (tty_bold, tty_yellow, " ".join(positive_found), tty_normal))
    if default_found:
        vverbose("   without scan function: %s%s%s%s\n" % (tty_bold, tty_blue, " ".join(default_found), tty_normal))


    found.sort()
    return found

def discover_check_type(hostname, ipaddress, check_type, use_caches, on_error):
    # Skip this check type if is ignored for that host
    if service_ignored(hostname, check_type, None):
        return []

    # Skip SNMP checks on non-SNMP hosts
    if check_uses_snmp(check_type) and not is_snmp_host(hostname):
        return []

    try:
        discovery_function = check_info[check_type]["inventory_function"]
        if discovery_function == None:
            discovery_function = no_discovery_possible
    except KeyError:
        raise MKGeneralException("No such check type '%s'" % check_type)

    section_name = check_type.split('.')[0]    # make e.g. 'lsi' from 'lsi.arrays'

    try:
        info = None # default in case of exception
        info = get_info_for_discovery(hostname, ipaddress, section_name, use_caches)
    except MKAgentError, e:
        if str(e) and str(e) != "Cannot get information from agent, processing only piggyback data.":
            raise
    except MKSNMPError, e:
        if str(e):
            raise
    except MKParseFunctionError, e:
        if opt_debug:
            raise

    if info == None: # No data for this check type
        return []

    # In case of SNMP checks but missing agent response, skip this check.
    # Special checks which still need to be called even with empty data
    # may declare this.
    if not info and check_uses_snmp(check_type) \
       and not check_info[check_type]["handle_empty_info"]:
        return []

    # Now do the actual inventory
    try:
        # Check number of arguments of discovery function. Note: This
        # check for the legacy API will be removed after 1.2.6.
        if len(inspect.getargspec(discovery_function).args) == 2:
            discovered_items = discovery_function(check_type, info) # discovery is a list of pairs (item, current_value)
        else:
            # New preferred style since 1.1.11i3: only one argument: info
            discovered_items = discovery_function(info)

        # tolerate function not explicitely returning []
        if discovered_items == None:
            discovered_items = []

        # New yield based api style
        elif type(discovered_items) != list:
            discovered_items = list(discovered_items)

        result = []
        for entry in discovered_items:
            if not isinstance(entry, tuple):
                sys.stderr.write("%s: Check %s returned invalid discovery data (entry not a tuple): %r\n" %
                                                                     (hostname, check_type, repr(entry)))
                continue

            if len(entry) == 2: # comment is now obsolete
                item, paramstring = entry
            else:
                try:
                    item, comment, paramstring = entry
                except ValueError:
                    sys.stderr.write("%s: Check %s returned invalid discovery data (not 2 or 3 elements): %r\n" %
                                                                           (hostname, check_type, repr(entry)))
                    continue

            # Check_MK 1.2.7i3 defines items to be unicode strings. Convert non unicode
            # strings here seamless. TODO remove this conversion one day and replace it
            # with a validation that item needs to be of type unicode
            if type(item) == str:
                item = decode_incoming_string(item)

            description = service_description(check_type, item)
            # make sanity check
            if len(description) == 0:
                sys.stderr.write("%s: Check %s returned empty service description - ignoring it.\n" %
                                                (hostname, check_type))
                continue

            result.append((item, paramstring))

    except Exception, e:
        if on_error == "warn":
            warning("  Exception in discovery function of check type '%s': %s" % (check_type, e))
        elif on_error == "raise":
            raise
        return []

    return result

def discoverable_check_types(what): # snmp, tcp, all
    check_types = [ k for k in check_info.keys()
                   if check_info[k]["inventory_function"] != None
                   and (what == "all"
                        or check_uses_snmp(k) == (what == "snmp"))
                 ]
    check_types.sort()
    return check_types


# Creates a table of all services that a host has or could have according
# to service discovery. The result is a dictionary of the form
# (check_type, item) -> (check_source, paramstring)
# check_source is the reason/state/source of the service:
#    "new"           : Check is discovered but currently not yet monitored
#    "old"           : Check is discovered and already monitored (most common)
#    "vanished"      : Check had been discovered previously, but item has vanished
#    "legacy"        : Check is defined via legacy_checks
#    "active"        : Check is defined via active_checks
#    "custom"        : Check is defined via custom_checks
#    "manual"        : Check is a manual Check_MK check without service discovery
#    "ignored"       : discovered or static, but disabled via ignored_services
#    "obsolete"      : Discovered by vanished check is meanwhile ignored via ignored_services
#    "clustered_new" : New service found on a node that belongs to a cluster
#    "clustered_old" : Old service found on a node that belongs to a cluster
# This function is cluster-aware
def get_host_services(hostname, use_caches, do_snmp_scan, on_error, ipaddress=None):
    if is_cluster(hostname):
        return get_cluster_services(hostname, use_caches, do_snmp_scan, on_error)
    else:
        return get_node_services(hostname, ipaddress, use_caches, do_snmp_scan, on_error)


# Part of get_node_services that deals with discovered services
def get_discovered_services(hostname, ipaddress, use_caches, do_snmp_scan, on_error):
    # Create a dict from check_type/item to check_source/paramstring
    services = {}

    # Handle discovered services -> "new"
    new_items = discover_services(hostname, None, use_caches, do_snmp_scan, on_error, ipaddress)
    for check_type, item, paramstring in new_items:
       services[(check_type, item)] = ("new", paramstring)

    # Match with existing items -> "old" and "vanished"
    old_items = parse_autochecks_file(hostname)
    for check_type, item, paramstring in old_items:
        if (check_type, item) not in services:
            services[(check_type, item)] = ("vanished", paramstring)
        else:
            services[(check_type, item)] = ("old", paramstring)

    return services

# Do the actual work for a non-cluster host or node
def get_node_services(hostname, ipaddress, use_caches, do_snmp_scan, on_error):
    services = get_discovered_services(hostname, ipaddress, use_caches, do_snmp_scan, on_error)

    # Identify clustered services
    for (check_type, item), (check_source, paramstring) in services.items():
        descr = service_description(check_type, item)
        if hostname != host_of_clustered_service(hostname, descr):
            if check_source == "vanished":
                del services[(check_type, item)] # do not show vanished clustered services here
            else:
                services[(check_type, item)] = ("clustered_" + check_source, paramstring)

    merge_manual_services(services, hostname)
    return services

# To a list of discovered services add/replace manual and active
# checks and handle ignoration
def merge_manual_services(services, hostname):
    # Find manual checks. These can override discovered checks -> "manual"
    manual_items = get_check_table(hostname, skip_autochecks=True)
    for (check_type, item), (params, descr, deps) in manual_items.items():
        services[(check_type, item)] = ('manual', repr(params) )

    # Add legacy checks -> "legacy"
    legchecks = host_extra_conf(hostname, legacy_checks)
    for cmd, descr, perf in legchecks:
        services[('legacy', descr)] = ('legacy', 'None')

    # Add custom checks -> "custom"
    custchecks = host_extra_conf(hostname, custom_checks)
    for entry in custchecks:
        services[('custom', entry['service_description'])] = ('custom', 'None')

    # Similar for 'active_checks', but here we have parameters
    for acttype, rules in active_checks.items():
        act_info = active_check_info[acttype]
        entries = host_extra_conf(hostname, rules)
        for params in entries:
            descr = act_info["service_description"](params)
            services[(acttype, descr)] = ('active', repr(params))

    # Handle disabled services -> "obsolete" and "ignored"
    for (check_type, item), (check_source, paramstring) in services.items():
        descr = service_description(check_type, item)
        if service_ignored(hostname, check_type, descr):
            if check_source == "vanished":
                new_source = "obsolete"
            else:
                new_source = "ignored"
            services[(check_type, item)] = (new_source, paramstring)

    return services

# Do the work for a cluster
def get_cluster_services(hostname, use_caches, with_snmp_scan, on_error):
    nodes = nodes_of(hostname)

    # Get services of the nodes. We are only interested in "old", "new" and "vanished"
    # From the states and parameters of these we construct the final state per service.
    cluster_items = {}
    for node in nodes:
        services = get_discovered_services(node, None, use_caches, with_snmp_scan, on_error)
        for (check_type, item), (check_source, paramstring) in services.items():
            descr = service_description(check_type, item)
            if hostname == host_of_clustered_service(node, descr):
                if (check_type, item) not in cluster_items:
                    cluster_items[(check_type, item)] = (check_source, paramstring)
                else:
                    first_check_source, first_paramstring = cluster_items[(check_type, item)]
                    if first_check_source == "old":
                        pass
                    elif check_source == "old":
                        cluster_items[(check_type, item)] = (check_source, paramstring)
                    elif first_check_source == "vanished" and check_source == "new":
                        cluster_items[(check_type, item)] = ("old", first_paramstring)
                    elif check_source == "vanished" and first_check_source == "new":
                        cluster_items[(check_type, item)] = ("old", paramstring)
                    # In all other cases either both must be "new" or "vanished" -> let it be

    # Now add manual and active serivce and handle ignored services
    merge_manual_services(cluster_items, hostname)
    return cluster_items


# Get the list of service of a host or cluster and guess the current state of
# all services if possible
def get_check_preview(hostname, use_caches, do_snmp_scan, on_error):
    services = get_host_services(hostname, use_caches, do_snmp_scan, on_error)
    if is_cluster(hostname):
        ipaddress = None
    else:
        ipaddress = lookup_ip_address(hostname)

    table = []
    for (check_type, item), (check_source, paramstring) in services.items():
        params = None
        if check_source not in [ 'legacy', 'active', 'custom' ]:
            # apply check_parameters
            try:
                if type(paramstring) == str:
                    params = eval(paramstring)
                else:
                    params = paramstring
            except:
                raise MKGeneralException("Invalid check parameter string '%s'" % paramstring)

            descr = service_description(check_type, item)
            global g_service_description
            g_service_description = descr
            infotype = check_type.split('.')[0]

            # Sorry. The whole caching stuff is the most horrible hack in
            # whole Check_MK. Nobody dares to clean it up, YET. But that
            # day is getting nearer...
            global opt_use_cachefile
            old_opt_use_cachefile = opt_use_cachefile
            opt_use_cachefile = True
            opt_dont_submit = True # hack for get_realhost_info, avoid skipping because of check interval

            if check_type not in check_info:
                continue # Skip not existing check silently

            try:
                exitcode = None
                perfdata = []
                info = get_info_for_check(hostname, ipaddress, infotype)
            # Handle cases where agent does not output data
            except MKAgentError, e:
                exitcode = 3
                output = "Error getting data from agent"
                if str(e):
                    output += ": %s" % e
                tcp_error = output

            except MKSNMPError, e:
                exitcode = 3
                output = "Error getting data from agent for %s via SNMP" % infotype
                if str(e):
                    output += ": %s" % e
                snmp_error = output

            except Exception, e:
                exitcode = 3
                output = "Error getting data for %s: %s" % (infotype, e)
                if check_uses_snmp(check_type):
                    snmp_error = output
                else:
                    tcp_error = output

            opt_use_cachefile = old_opt_use_cachefile

            global g_check_type, g_checked_item
            g_check_type = check_type
            g_checked_item = item

            if exitcode == None:
                check_function = check_info[check_type]["check_function"]
                if check_source != 'manual':
                    params = get_precompiled_check_parameters(hostname, item, compute_check_parameters(hostname, check_type, item, params), check_type)

                try:
                    reset_wrapped_counters()
                    result = sanitize_check_result(check_function(item, params, info), check_uses_snmp(check_type))
                    if last_counter_wrap():
                        raise last_counter_wrap()
                except MKCounterWrapped, e:
                    result = (None, "WAITING - Counter based check, cannot be done offline")
                except Exception, e:
                    if opt_debug:
                        raise
                    result = (3, "UNKNOWN - invalid output from agent or error in check implementation")
                if len(result) == 2:
                    result = (result[0], result[1], [])
                exitcode, output, perfdata = result
        else:
            descr = item
            exitcode = None
            output = "WAITING - %s check, cannot be done offline" % check_source.title()
            perfdata = []

        if check_source == "active":
            params = eval(paramstring)

        if check_source in [ "legacy", "active", "custom" ]:
            checkgroup = None
            if service_ignored(hostname, None, descr):
                check_source = "ignored"
        else:
            checkgroup = check_info[check_type]["group"]

        table.append((check_source, check_type, checkgroup, item, paramstring, params, descr, exitcode, output, perfdata))

    return table



#.
#   .--Autochecks----------------------------------------------------------.
#   |            _         _             _               _                 |
#   |           / \  _   _| |_ ___   ___| |__   ___  ___| | _____          |
#   |          / _ \| | | | __/ _ \ / __| '_ \ / _ \/ __| |/ / __|         |
#   |         / ___ \ |_| | || (_) | (__| | | |  __/ (__|   <\__ \         |
#   |        /_/   \_\__,_|\__\___/ \___|_| |_|\___|\___|_|\_\___/         |
#   |                                                                      |
#   +----------------------------------------------------------------------+
#   |  Reading, parsing, writing, modifying autochecks files               |
#   '----------------------------------------------------------------------'

# Read automatically discovered checks of one host.
# world: "config" -> File in var/check_mk/autochecks
#        "active" -> Copy in var/check_mk/core/autochecks
# Returns a table with three columns:
# 1. check_type
# 2. item
# 3. parameters evaluated!
def read_autochecks_of(hostname, world="config"):
    if world == "config":
        basedir = autochecksdir
    else:
        basedir = var_dir + "/core/autochecks"
    filepath = basedir + '/' + hostname + '.mk'

    if not os.path.exists(filepath):
        return []
    try:
        autochecks_raw = eval(file(filepath).read())
    except SyntaxError,e:
        if opt_verbose or opt_debug:
            sys.stderr.write("Syntax error in file %s: %s\n" % (filepath, e))
        if opt_debug:
            raise
        return []
    except Exception, e:
        if opt_verbose or opt_debug:
            sys.stderr.write("Error in file %s:\n%s\n" % (filepath, e))
        if opt_debug:
            raise
        return []

    # Exchange inventorized check parameters with those configured by
    # the user. Also merge with default levels for modern dictionary based checks.
    autochecks = []
    for entry in autochecks_raw:
        if len(entry) == 4: # old format where hostname is at the first place
            entry = entry[1:]
        check_type, item, parameters = entry
        # With Check_MK 1.2.7i3 items are now defined to be unicode strings. Convert
        # items from existing autocheck files for compatibility. TODO remove this one day
        if type(item) == str:
            item = decode_incoming_string(item)
        autochecks.append((check_type, item, compute_check_parameters(hostname, check_type, item, parameters)))
    return autochecks


# Read autochecks, but do not compute final check parameters,
# also return a forth column with the raw string of the parameters.
# Returns a table with three columns:
# 1. check_type
# 2. item
# 3. parameter string, not yet evaluated!
def parse_autochecks_file(hostname):
    def split_python_tuple(line):
        quote = None
        bracklev = 0
        backslash = False
        for i, c in enumerate(line):
            if backslash:
                backslash = False
                continue
            elif c == '\\':
                backslash = True
            elif c == quote:
                quote = None # end of quoted string
            elif c in [ '"', "'" ]:
                quote = c # begin of quoted string
            elif quote:
                continue
            elif c in [ '(', '{', '[' ]:
                bracklev += 1
            elif c in [ ')', '}', ']' ]:
                bracklev -= 1
            elif bracklev > 0:
                continue
            elif c == ',':
                value = line[0:i]
                rest = line[i+1:]
                return value.strip(), rest
        return line.strip(), None

    path = "%s/%s.mk" % (autochecksdir, hostname)
    if not os.path.exists(path):
        return []
    lineno = 0

    table = []
    for line in file(path):
        lineno += 1
        try:
            line = line.strip()
            if not line.startswith("("):
                continue

            # drop everything after potential '#' (from older versions)
	    i = line.rfind('#')
	    if i > 0: # make sure # is not contained in string
		rest = line[i:]
		if '"' not in rest and "'" not in rest:
		    line = line[:i].strip()

            if line.endswith(","):
                line = line[:-1]
            line = line[1:-1] # drop brackets

            # First try old format - with hostname
            parts = []
            while True:
                try:
                    part, line = split_python_tuple(line)
                    parts.append(part)
                except:
                    break
            if len(parts) == 4:
                parts = parts[1:] # drop hostname, legacy format with host in first column
            elif len(parts) != 3:
                raise Exception("Invalid number of parts: %d" % len(parts))

            checktypestring, itemstring, paramstring = parts

            item = eval(itemstring)
            # With Check_MK 1.2.7i3 items are now defined to be unicode strings. Convert
            # items from existing autocheck files for compatibility. TODO remove this one day
            if type(item) == str:
                item = decode_incoming_string(item)

            table.append((eval(checktypestring), item, paramstring))
        except:
            if opt_debug:
                raise
            raise Exception("Invalid line %d in autochecks file %s" % (lineno, path))
    return table


def has_autochecks(hostname):
    return os.path.exists(autochecksdir + "/" + hostname + ".mk")


def save_autochecks_file(hostname, items):
    if not os.path.exists(autochecksdir):
        os.makedirs(autochecksdir)
    filepath = "%s/%s.mk" % (autochecksdir, hostname)
    out = file(filepath, "w")
    out.write("[\n")
    for check_type, item, paramstring in items:
        out.write("  (%r, %r, %s),\n" % (check_type, item, paramstring))
    out.write("]\n")


def set_autochecks_of(hostname, new_items):
    # A Cluster does not have an autochecks file
    # All of its services are located in the nodes instead
    # So we cycle through all nodes remove all clustered service
    # and add the ones we've got from stdin
    if is_cluster(hostname):
        for node in nodes_of(hostname):
            new_autochecks = []
            existing = parse_autochecks_file(node)
            for check_type, item, paramstring in existing:
                descr = service_description(check_type, item)
                if hostname != host_of_clustered_service(node, descr):
                    new_autochecks.append((check_type, item, paramstring))
            for (check_type, item), paramstring in new_items.items():
                new_autochecks.append((check_type, item, paramstring))
            # write new autochecks file for that host
            save_autochecks_file(node, new_autochecks)
    else:
        existing = parse_autochecks_file(hostname)
        # write new autochecks file, but take paramstrings from existing ones
        # for those checks which are kept
        new_autochecks = []
        for ct, item, paramstring in existing:
            if (ct, item) in new_items:
                new_autochecks.append((ct, item, paramstring))
                del new_items[(ct, item)]

        for (ct, item), paramstring in new_items.items():
            new_autochecks.append((ct, item, paramstring))

        # write new autochecks file for that host
        save_autochecks_file(hostname, new_autochecks)


# Remove all autochecks of a host while being cluster-aware!
def remove_autochecks_of(hostname):
    removed = 0
    nodes = nodes_of(hostname)
    if nodes:
        for node in nodes:
            old_items = parse_autochecks_file(node)
            new_items = []
            for check_type, item, paramstring in old_items:
                descr = service_description(check_type, item)
                if hostname != host_of_clustered_service(node, descr):
                    new_items.append((check_type, item, paramstring))
                else:
                    removed += 1
            save_autochecks_file(node, new_items)
    else:
        old_items = parse_autochecks_file(hostname)
        new_items = []
        for check_type, item, paramstring in old_items:
            descr = service_description(check_type, item)
            if hostname != host_of_clustered_service(hostname, descr):
                new_items.append((check_type, item, paramstring))
            else:
                removed += 1
        save_autochecks_file(hostname, new_items)

    return removed

